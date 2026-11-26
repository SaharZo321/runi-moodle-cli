"""Reading text from Moodle course files / Pages and downloading files to disk."""

from __future__ import annotations

import io
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from docx import Document
from pptx import Presentation
from pypdf import PdfReader

from runi_moodle_cli.client import MoodleError, _http, call
from runi_moodle_cli.config import configured_years, get_site
from runi_moodle_cli.courses import get_course_contents
from runi_moodle_cli.formatting import html_to_text, sanitize_filename

MAX_CHARS = 30_000
MAX_READ_BYTES = 25 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 250 * 1024 * 1024

PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _site_year(url: str) -> int:
    """Resolve which configured year's Moodle site a URL belongs to.
    Tokens are only ever sent to a configured Moodle host/year."""
    for year in configured_years():
        if url.startswith(get_site(year).url + "/"):
            return year
    raise ValueError(
        "Only links to your configured Moodle site can be accessed "
        "(e.g. file URLs from `runi course <course> contents`)."
    )


def _ws_file_url(url: str) -> str:
    return re.sub(r"/(webservice/)?pluginfile\.php", "/webservice/pluginfile.php", url, count=1)


def _page_text(cmid: int, year: int) -> tuple[str, str]:
    pages = call("mod_page_get_pages_by_courses", year=year)["pages"]
    page = next((p for p in pages if p["coursemodule"] == cmid), None)
    if page is None:
        raise ValueError(f"No Page activity with id {cmid}")
    return page["name"], html_to_text(page["intro"] + "\n\n" + page["content"])


def _pdf(data: bytes) -> str:
    pages = PdfReader(io.BytesIO(data)).pages
    return "\n\n".join(f"--- page {i} ---\n{p.extract_text() or ''}" for i, p in enumerate(pages, 1))


def _pptx(data: bytes) -> str:
    slides = []
    for i, slide in enumerate(Presentation(io.BytesIO(data)).slides, 1):
        lines = [
            shape.text_frame.text
            for shape in slide.shapes
            if shape.has_text_frame and shape.text_frame.text.strip()
        ]
        if slide.has_notes_slide and (notes := slide.notes_slide.notes_text_frame.text.strip()):
            lines.append(f"[notes] {notes}")
        slides.append(f"--- slide {i} ---\n" + "\n".join(lines))
    return "\n\n".join(slides)


def _docx(data: bytes) -> str:
    doc = Document(io.BytesIO(data))
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        parts += [" | ".join(cell.text.strip() for cell in row.cells) for row in table.rows]
    return "\n".join(parts)


def _notebook(notebook: dict) -> str:
    cells = []
    for i, cell in enumerate(notebook.get("cells", []), 1):
        source = cell.get("source", "")
        text = "".join(source) if isinstance(source, list) else source
        cells.append(f"--- cell {i} ({cell.get('cell_type')}) ---\n{text}")
    return "\n\n".join(cells)


def _file_text(url: str, year: int) -> tuple[str, str]:
    ws_url = _ws_file_url(url)
    response = _http.get(ws_url, params={"token": get_site(year).token}, follow_redirects=True)
    response.raise_for_status()
    kind = response.headers.get("content-type", "").split(";")[0].strip()
    name = unquote(urlparse(url).path.rsplit("/", 1)[-1])
    if len(response.content) > MAX_READ_BYTES:
        raise ValueError("File is larger than 25 MB; download it with `runi download` instead.")

    if kind == "application/json":
        data = response.json()
        if name.endswith(".ipynb") and isinstance(data, dict) and "cells" in data:
            return name, _notebook(data)
        if isinstance(data, dict) and "error" in data:
            raise MoodleError(data["error"])
        return name, response.text
    if kind == "application/pdf":
        return name, _pdf(response.content)
    if kind == PPTX:
        return name, _pptx(response.content)
    if kind == DOCX:
        return name, _docx(response.content)
    if kind.startswith("text/"):
        text = response.text.lstrip("\ufeff")
        return name, html_to_text(text) if kind == "text/html" else text
    raise ValueError(
        f"Can't extract text from {kind or 'this type of'} files; supported: PDF, PowerPoint (.pptx), "
        "Word (.docx), notebooks (.ipynb), text, code and HTML. Use `runi download` to save the file."
    )


def read_content(url: str, offset: int = 0) -> dict:
    year = _site_year(url)
    path = urlparse(url).path
    if "/mod/page/view.php" in path:
        cmid = int(parse_qs(urlparse(url).query)["id"][0])
        name, text = _page_text(cmid, year)
    elif "pluginfile.php" in path:
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


def download_file(
    url: str,
    dest: Path | None = None,
    overwrite: bool = False,
) -> dict:
    """Download a Moodle pluginfile.php URL directly to disk."""
    year = _site_year(url)
    if "pluginfile.php" not in urlparse(url).path:
        raise ValueError("Pass a Moodle file URL (.../pluginfile.php/...).")

    raw_name = unquote(urlparse(url).path.rsplit("/", 1)[-1]) or "download"
    safe_name = sanitize_filename(raw_name, fallback="download")

    if dest is None:
        out_path = Path.cwd() / safe_name
    elif dest.exists() and dest.is_dir():
        out_path = dest / safe_name
    elif str(dest).endswith(("/", "\\")):
        dest.mkdir(parents=True, exist_ok=True)
        out_path = dest / safe_name
    else:
        out_path = dest

    if out_path.exists() and not overwrite:
        return {"path": str(out_path), "filename": out_path.name, "bytes": out_path.stat().st_size, "skipped": True}

    ws_url = _ws_file_url(url)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with _http.stream("GET", ws_url, params={"token": get_site(year).token}, follow_redirects=True) as response:
        response.raise_for_status()
        kind = response.headers.get("content-type", "").split(";")[0].strip()
        if kind == "application/json" and not safe_name.endswith((".json", ".ipynb")):
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
            if isinstance(payload, dict) and "error" in payload:
                raise MoodleError(payload["error"])
            data = json.dumps(payload, indent=2).encode("utf-8")
            out_path.write_bytes(data)
            return {"path": str(out_path), "filename": out_path.name, "bytes": len(data), "skipped": False}

        total = 0
        tmp_path = out_path.with_suffix(out_path.suffix + ".part")
        try:
            with tmp_path.open("wb") as fh:
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise ValueError(f"File exceeds {MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB download limit.")
                    fh.write(chunk)
            tmp_path.replace(out_path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

    return {"path": str(out_path), "filename": out_path.name, "bytes": total, "skipped": False}


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
    for sec in sections:
        sec_name = sec["section"] or "General"
        if needle and needle not in sec_name.lower():
            continue
        sec_dir = dest_dir / sanitize_filename(sec_name, fallback="section")
        for item in sec["items"]:
            for f in item.get("files", []):
                target = sec_dir / sanitize_filename(f["filename"], fallback="file")
                try:
                    res = download_file(f["url"], dest=target, overwrite=overwrite)
                except Exception as exc:
                    errors.append({"filename": f["filename"], "url": f["url"], "error": str(exc)})
                else:
                    if res["skipped"]:
                        skipped.append(res)
                    else:
                        downloaded.append(res)

    return {"downloaded": downloaded, "skipped": skipped, "errors": errors}
