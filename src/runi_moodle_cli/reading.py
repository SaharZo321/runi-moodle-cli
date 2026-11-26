"""Reading text from Moodle course files / Pages and downloading files to disk."""

from __future__ import annotations

import contextlib
import io
import json
import os
import posixpath
import re
import shutil
import tempfile
from pathlib import Path
from typing import Iterator
from urllib.parse import parse_qs, parse_qsl, unquote, urlencode, urljoin, urlparse, urlunparse

import httpx
from docx import Document
from pptx import Presentation
from pypdf import PdfReader

from runi_moodle_cli.client import MoodleError, _http, call, sanitize_http_error, sanitize_url
from runi_moodle_cli.config import AuthError, configured_years, get_site
from runi_moodle_cli.courses import get_course_contents
from runi_moodle_cli.formatting import MAX_FILENAME_BYTES, html_to_text, sanitize_filename, unescape_text

MAX_CHARS = 30_000
MAX_READ_BYTES = 25 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 250 * 1024 * 1024
MAX_EXTRACTED_CHARS = 2_000_000
MAX_REDIRECTS = 5

PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _origin(url: str | httpx.URL) -> tuple[str, str, int]:
    parsed = urlparse(str(url))
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port if parsed.port is not None else (443 if scheme == "https" else 80)
    except ValueError:
        port = -1
    return scheme, host, port


def _site_year(url: str) -> int:
    """Resolve which configured year's Moodle site a URL belongs to.
    Tokens are only ever sent to a configured HTTPS Moodle origin/year."""
    parsed = urlparse(url)
    decoded_path = unquote(parsed.path or "/")
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or ".." in decoded_path.split("/")
    ):
        raise ValueError(
            "Only https:// links to your configured Moodle site can be accessed "
            "(e.g. file URLs from `runi course <course> contents`)."
        )
    req_origin = _origin(url)
    norm_path = posixpath.normpath(decoded_path)
    for year in configured_years():
        site = get_site(year)
        site_parsed = urlparse(site.url)
        site_prefix = posixpath.normpath(site_parsed.path or "/").rstrip("/") + "/"
        if req_origin == _origin(site.url) and norm_path.startswith(site_prefix):
            return year
    raise ValueError(
        "Only links to your configured Moodle site can be accessed "
        "(e.g. file URLs from `runi course <course> contents`)."
    )


def _ws_file_url(url: str, token: str) -> str:
    parsed = urlparse(url)
    new_path = re.sub(r"/(webservice/)?pluginfile\.php", "/webservice/pluginfile.php", parsed.path, count=1)
    query_pairs = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k.lower() != "token"]
    query_pairs.append(("token", token))
    return urlunparse(parsed._replace(path=new_path, query=urlencode(query_pairs), fragment=""))


@contextlib.contextmanager
def _open_file_stream(url: str, year: int) -> Iterator[httpx.Response]:
    """Stream a Moodle file with strict same-origin redirect checks and token-safe errors."""
    site = get_site(year)
    expected_origin = _origin(site.url)
    current_url = _ws_file_url(url, site.token)

    for hop in range(MAX_REDIRECTS + 1):
        try:
            cm = _http.stream("GET", current_url, follow_redirects=False)
            response = cm.__enter__()
        except httpx.HTTPError as exc:
            raise MoodleError(f"Could not download file: {sanitize_http_error(exc)}") from None

        if response.status_code in (301, 302, 303, 307, 308):
            location = response.headers.get("location", "").strip()
            cm.__exit__(None, None, None)
            if not location or hop == MAX_REDIRECTS:
                raise MoodleError(f"Redirect failed while fetching {sanitize_url(url)}.")
            next_url = urljoin(current_url, location)
            next_parsed = urlparse(next_url)
            if (
                next_parsed.scheme.lower() != "https"
                or next_parsed.username is not None
                or next_parsed.password is not None
                or _origin(next_url) != expected_origin
            ):
                raise ValueError(
                    f"Refusing cross-origin or insecure redirect to {sanitize_url(next_url)}."
                )
            current_url = next_url
            continue

        if response.status_code >= 400:
            status = response.status_code
            cm.__exit__(None, None, None)
            if status in (401, 403):
                raise AuthError(f"HTTP {status} fetching {sanitize_url(url)}. Check your Moodle login.")
            raise MoodleError(f"HTTP {status} fetching {sanitize_url(url)}.")

        try:
            yield response
        finally:
            cm.__exit__(None, None, None)
        return

    raise MoodleError(f"Too many redirects fetching {sanitize_url(url)}.")


def _check_json_error(payload: object) -> None:
    if isinstance(payload, dict) and ("error" in payload or "exception" in payload):
        code = str(payload.get("errorcode") or "")
        msg = str(payload.get("error") or payload.get("message") or "Moodle file error")
        if code == "invalidtoken" or "invalid token" in msg.lower():
            raise AuthError(msg)
        raise MoodleError(msg)


def _read_bounded(response: httpx.Response, limit_bytes: int, label: str) -> bytes:
    cl = response.headers.get("content-length", "").strip()
    if cl.isdigit() and int(cl) > limit_bytes:
        raise ValueError(label)
    buf = bytearray()
    for chunk in response.iter_bytes():
        buf.extend(chunk)
        if len(buf) > limit_bytes:
            raise ValueError(label)
    return bytes(buf)


def _page_text(cmid: int, year: int) -> tuple[str, str]:
    pages = call("mod_page_get_pages_by_courses", year=year).get("pages", [])
    page = next((p for p in pages if p.get("coursemodule") == cmid), None)
    if page is None:
        raise ValueError(f"No Page activity with id {cmid}")
    name = unescape_text(page.get("name") or f"Page {cmid}")
    body = html_to_text((page.get("intro") or "") + "\n\n" + (page.get("content") or ""))
    return name, body


def _pdf(data: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ValueError("PDF is password-protected and cannot be read.")
        parts: list[str] = []
        total = 0
        for i, page in enumerate(reader.pages, 1):
            chunk = f"--- page {i} ---\n{page.extract_text() or ''}"
            parts.append(chunk)
            total += len(chunk)
            if total >= MAX_EXTRACTED_CHARS:
                break
        return "\n\n".join(parts)[:MAX_EXTRACTED_CHARS]
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"Could not parse PDF file: {exc}") from exc


def _pptx(data: bytes) -> str:
    try:
        slides: list[str] = []
        total = 0
        for i, slide in enumerate(Presentation(io.BytesIO(data)).slides, 1):
            lines = [
                shape.text_frame.text
                for shape in slide.shapes
                if shape.has_text_frame and shape.text_frame.text.strip()
            ]
            if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
                if notes := slide.notes_slide.notes_text_frame.text.strip():
                    lines.append(f"[notes] {notes}")
            chunk = f"--- slide {i} ---\n" + "\n".join(lines)
            slides.append(chunk)
            total += len(chunk)
            if total >= MAX_EXTRACTED_CHARS:
                break
        return "\n\n".join(slides)[:MAX_EXTRACTED_CHARS]
    except Exception as exc:
        raise ValueError(f"Could not parse PowerPoint file: {exc}") from exc


def _docx(data: bytes) -> str:
    try:
        doc = Document(io.BytesIO(data))
        parts = [p.text for p in doc.paragraphs if p.text.strip()]
        for table in doc.tables:
            parts += [" | ".join(cell.text.strip() for cell in row.cells) for row in table.rows]
        return "\n".join(parts)[:MAX_EXTRACTED_CHARS]
    except Exception as exc:
        raise ValueError(f"Could not parse Word document: {exc}") from exc


def _notebook(notebook: dict) -> str:
    cells = []
    for i, cell in enumerate(notebook.get("cells", []), 1):
        source = cell.get("source", "")
        text = "".join(source) if isinstance(source, list) else str(source)
        cells.append(f"--- cell {i} ({cell.get('cell_type')}) ---\n{text}")
    return "\n\n".join(cells)[:MAX_EXTRACTED_CHARS]


def _file_text(url: str, year: int) -> tuple[str, str]:
    name = unquote(urlparse(url).path.rsplit("/", 1)[-1]) or "file"
    with _open_file_stream(url, year) as response:
        kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
        content = _read_bounded(
            response,
            MAX_READ_BYTES,
            "File is larger than 25 MB; download it with `runi download` instead.",
        )

    if kind == "application/json":
        try:
            data = json.loads(content.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Could not parse JSON file: {exc}") from exc
        _check_json_error(data)
        if name.lower().endswith(".ipynb") and isinstance(data, dict) and "cells" in data:
            return name, _notebook(data)
        return name, content.decode("utf-8", errors="replace")
    if kind == "application/pdf":
        return name, _pdf(content)
    if kind == PPTX:
        return name, _pptx(content)
    if kind == DOCX:
        return name, _docx(content)
    if kind.startswith("text/"):
        text = content.decode("utf-8", errors="replace").lstrip("\ufeff")
        return name, html_to_text(text) if kind == "text/html" else text
    raise ValueError(
        f"Can't extract text from {kind or 'this type of'} files; supported: PDF, PowerPoint (.pptx), "
        "Word (.docx), notebooks (.ipynb), text, code and HTML. Use `runi download` to save the file."
    )


def read_content(url: str, offset: int = 0) -> dict:
    if offset < 0:
        raise ValueError("offset must be 0 or greater.")
    year = _site_year(url)
    parsed = urlparse(url)
    path = parsed.path
    if "/mod/page/view.php" in path:
        id_values = parse_qs(parsed.query).get("id")
        if not id_values or not id_values[0].isdigit():
            raise ValueError("Page URL must include a numeric ?id=<cmid> query parameter.")
        cmid = int(id_values[0])
        name, text = _page_text(cmid, year)
    elif "/pluginfile.php/" in path or "/webservice/pluginfile.php/" in path:
        name, text = _file_text(url, year)
    else:
        raise ValueError(
            "Pass a file URL (.../pluginfile.php/...) or a Page URL (.../mod/page/view.php?id=...)."
        )
    part = text[offset : offset + MAX_CHARS]
    end = offset + len(part)
    return {
        "name": name,
        "text": part,
        "total_chars": len(text),
        **({"next_offset": end} if end < len(text) else {}),
    }


def _publish_temp_file(tmp_path: Path, out_path: Path, *, overwrite: bool) -> bool:
    """Publish tmp_path to out_path. Returns True if skipped because destination already exists."""
    if out_path.is_symlink():
        raise ValueError(f"Refusing to write to symlink destination: {out_path}")
    if overwrite:
        os.replace(tmp_path, out_path)
        return False
    if os.name == "nt":
        try:
            os.rename(tmp_path, out_path)
            return False
        except FileExistsError:
            tmp_path.unlink(missing_ok=True)
            return True
    if hasattr(os, "link"):
        try:
            os.link(tmp_path, out_path)
            tmp_path.unlink(missing_ok=True)
            return False
        except FileExistsError:
            tmp_path.unlink(missing_ok=True)
            return True
        except OSError:
            pass
    # Atomic no-overwrite fallback when hard links are unavailable on the filesystem
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        dst_fd = os.open(out_path, flags, 0o644)
    except FileExistsError:
        tmp_path.unlink(missing_ok=True)
        return True
    try:
        with os.fdopen(dst_fd, "wb") as dst_fh, tmp_path.open("rb") as src_fh:
            shutil.copyfileobj(src_fh, dst_fh)
    except BaseException:
        out_path.unlink(missing_ok=True)
        raise
    finally:
        tmp_path.unlink(missing_ok=True)
    return False


def download_file(
    url: str,
    dest: Path | None = None,
    overwrite: bool = False,
    *,
    dest_is_dir: bool = False,
) -> dict:
    """Download a Moodle pluginfile.php URL directly to disk."""
    year = _site_year(url)
    parsed = urlparse(url)
    if "/pluginfile.php/" not in parsed.path and "/webservice/pluginfile.php/" not in parsed.path:
        raise ValueError("Pass a Moodle file URL (.../pluginfile.php/...).")

    raw_name = unquote(parsed.path.rsplit("/", 1)[-1]) or "download"
    safe_name = sanitize_filename(raw_name, fallback="download")

    if dest is None:
        out_path = Path.cwd() / safe_name
    elif dest_is_dir or (dest.exists() and dest.is_dir()) or str(dest).endswith(("/", "\\")):
        dest.mkdir(parents=True, exist_ok=True)
        out_path = dest / safe_name
    else:
        out_path = dest

    if out_path.is_symlink():
        raise ValueError(f"Refusing to write to symlink destination: {out_path}")
    if out_path.exists() and not overwrite:
        return {"path": str(out_path), "filename": out_path.name, "bytes": out_path.stat().st_size, "skipped": True}

    out_path.parent.mkdir(parents=True, exist_ok=True)
    limit_msg = f"File exceeds {MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB download limit."

    with _open_file_stream(url, year) as response:
        kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
        cl = response.headers.get("content-length", "").strip()
        if cl.isdigit() and int(cl) > MAX_DOWNLOAD_BYTES:
            raise ValueError(limit_msg)

        fd, tmp_name = tempfile.mkstemp(prefix=".runi-dl-", suffix=".part", dir=str(out_path.parent))
        tmp_path = Path(tmp_name)
        total = 0
        try:
            if kind == "application/json" and not safe_name.lower().endswith((".json", ".ipynb")):
                os.close(fd)
                raw = _read_bounded(response, MAX_DOWNLOAD_BYTES, limit_msg)
                try:
                    payload = json.loads(raw.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    data = raw
                else:
                    _check_json_error(payload)
                    data = json.dumps(payload, indent=2).encode("utf-8")
                tmp_path.write_bytes(data)
                total = len(data)
            else:
                with os.fdopen(fd, "wb") as fh:
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > MAX_DOWNLOAD_BYTES:
                            raise ValueError(limit_msg)
                        fh.write(chunk)
            skipped = _publish_temp_file(tmp_path, out_path, overwrite=overwrite)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

    if skipped:
        return {"path": str(out_path), "filename": out_path.name, "bytes": out_path.stat().st_size, "skipped": True}
    return {"path": str(out_path), "filename": out_path.name, "bytes": total, "skipped": False}


def _unique_target(sec_dir: Path, filename: str, used: set[tuple[str, str]]) -> Path:
    sec_key = str(sec_dir).lower()
    base = sanitize_filename(filename, fallback="file")
    key = (sec_key, base.lower())
    if key not in used:
        used.add(key)
        return sec_dir / base
    stem, dot, ext = base.rpartition(".")
    if not dot or not stem or len(ext.encode("utf-8")) >= 20:
        stem, ext_part = base, ""
    else:
        ext_part = "." + ext
    ext_bytes = len(ext_part.encode("utf-8"))
    idx = 2
    while True:
        suffix = f" ({idx})"
        budget = max(1, MAX_FILENAME_BYTES - ext_bytes - len(suffix.encode("utf-8")))
        trimmed_stem = stem.encode("utf-8")[:budget].decode("utf-8", errors="ignore").rstrip(". ") or "file"
        candidate_name = f"{trimmed_stem}{suffix}{ext_part}"
        key = (sec_key, candidate_name.lower())
        if key not in used:
            used.add(key)
            return sec_dir / candidate_name
        idx += 1


def download_course_files(
    course_id: int,
    dest_dir: Path,
    year: int | None = None,
    section_filter: str | None = None,
    overwrite: bool = False,
) -> dict:
    """Download all files from a course into <dest_dir>/<section>/<filename>."""
    sections = get_course_contents(course_id, year=year)
    downloaded: list[dict] = []
    skipped: list[dict] = []
    errors: list[dict] = []

    needle = section_filter.lower().strip() if section_filter else None
    used_paths: set[tuple[str, str]] = set()
    for sec in sections:
        sec_name = sec["section"] or "General"
        if needle and needle not in sec_name.lower():
            continue
        sec_dir = dest_dir / sanitize_filename(sec_name, fallback="section")
        for item in sec["items"]:
            for f in item.get("files", []):
                target = _unique_target(sec_dir, f["filename"], used_paths)
                try:
                    res = download_file(f["url"], dest=target, overwrite=overwrite)
                except httpx.HTTPError as exc:
                    errors.append(
                        {"filename": f["filename"], "url": sanitize_url(f["url"]), "error": sanitize_http_error(exc)}
                    )
                except Exception as exc:
                    errors.append({"filename": f["filename"], "url": sanitize_url(f["url"]), "error": str(exc)})
                else:
                    if res["skipped"]:
                        skipped.append(res)
                    else:
                        downloaded.append(res)

    return {"downloaded": downloaded, "skipped": skipped, "errors": errors}
