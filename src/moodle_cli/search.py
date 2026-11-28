"""Moodle site-wide search across enrolled courses."""

from __future__ import annotations

from moodle_cli.client import call
from moodle_cli.formatting import fmt_date, html_to_text

SNIPPET = 300


def search(query: str, page: int = 0, year: int | None = None) -> dict:
    cleaned = (query or "").strip()
    if not cleaned:
        raise ValueError("Search query cannot be empty.")
    if page < 0:
        raise ValueError("page must be 0 or greater.")
    result = call("core_search_get_results", year=year, query=cleaned, page=page)
    return {
        "total": result.get("totalcount", 0),
        "page": page,
        "results": [
            {
                "title": html_to_text(r.get("title", "")),
                "kind": f"{r.get('componentname')}/{r.get('areaname')}",
                "course": html_to_text(r.get("coursefullname", "")),
                "snippet": html_to_text(r.get("content", ""))[:SNIPPET],
                "modified": fmt_date(int(r.get("timemodified") or 0)),
                "url": r.get("docurl"),
            }
            for r in result.get("results", [])
        ],
    }
