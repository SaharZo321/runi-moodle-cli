"""Shared formatting helpers for Moodle API data."""

from __future__ import annotations

import html
import re
from datetime import datetime

INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
BIDI_CONTROL_CHARS = re.compile(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]")
SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>[\s\S]*?</\1>", re.I)
WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
MAX_FILENAME_BYTES = 200


def fmt_date(ts: int) -> str | None:
    """Convert Unix timestamp to local YYYY-MM-DD HH:MM; 0 means unset."""
    if not ts:
        return None
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def html_to_text(raw: str) -> str:
    """Convert Moodle HTML fragments into clean plain text."""
    text = SCRIPT_STYLE_RE.sub("", raw or "")
    text = re.sub(r"<br\s*/?>|</p>|</div>|</li>", "\n", text, flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def unescape_text(raw: str | None) -> str:
    return html.unescape((raw or "").strip())


def browser_url(fileurl: str) -> str:
    """Convert webservice/pluginfile.php links to plain browser-openable pluginfile.php links."""
    return fileurl.replace("/webservice/pluginfile.php", "/pluginfile.php")


def file_list(fileareas: list[dict]) -> list[dict]:
    return [
        {"filename": f["filename"], "url": browser_url(f["fileurl"])}
        for area in fileareas
        for f in area.get("files", [])
        if f.get("filename") and f.get("fileurl") and not str(f["filename"]).startswith("image_page")
    ]


def _truncate_utf8(name: str, max_bytes: int) -> str:
    encoded = name.encode("utf-8")
    if len(encoded) <= max_bytes:
        return name
    stem, dot, ext = name.rpartition(".")
    if dot and stem and len(ext.encode("utf-8")) < 20:
        ext_part = "." + ext
        budget = max_bytes - len(ext_part.encode("utf-8"))
        truncated_stem = stem.encode("utf-8")[: max(1, budget)].decode("utf-8", errors="ignore").rstrip(". ")
        return (truncated_stem or "file") + ext_part
    return encoded[:max_bytes].decode("utf-8", errors="ignore").rstrip(". ") or "file"


def sanitize_filename(name: str, fallback: str = "file") -> str:
    """Make a filename or directory name safe on Windows, macOS, and Linux."""
    cleaned = BIDI_CONTROL_CHARS.sub("", name or "")
    cleaned = INVALID_FILENAME_CHARS.sub("_", cleaned.strip())
    cleaned = cleaned.strip(". ")
    if not cleaned:
        return fallback
    stem = cleaned.split(".", 1)[0].upper()
    if stem in WINDOWS_RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    return _truncate_utf8(cleaned, MAX_FILENAME_BYTES)
