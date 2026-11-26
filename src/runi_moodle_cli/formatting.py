"""Shared formatting helpers for Moodle API data."""

from __future__ import annotations

import html
import re
from datetime import datetime

INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')


def fmt_date(ts: int) -> str | None:
    """Convert Unix timestamp to local YYYY-MM-DD HH:MM; 0 means unset."""
    if not ts:
        return None
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def html_to_text(raw: str) -> str:
    """Convert Moodle HTML fragments into clean plain text."""
    raw = re.sub(r"<br\s*/?>|</p>|</div>|</li>", "\n", raw or "", flags=re.I)
    raw = html.unescape(re.sub(r"<[^>]+>", "", raw))
    return re.sub(r"\n\s*\n+", "\n\n", raw).strip()


def browser_url(fileurl: str) -> str:
    """Convert webservice/pluginfile.php links to plain browser-openable pluginfile.php links."""
    return fileurl.replace("/webservice/pluginfile.php", "/pluginfile.php")


def file_list(fileareas: list[dict]) -> list[dict]:
    return [
        {"filename": f["filename"], "url": browser_url(f["fileurl"])}
        for area in fileareas
        for f in area.get("files", [])
        if not f["filename"].startswith("image_page")
    ]


def sanitize_filename(name: str, fallback: str = "file") -> str:
    """Make a filename or directory name safe on Windows, macOS, and Linux."""
    cleaned = INVALID_FILENAME_CHARS.sub("_", (name or "").strip())
    cleaned = cleaned.strip(". ")
    return cleaned or fallback
