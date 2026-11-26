"""Upcoming actionable deadlines from Moodle's timeline."""

from __future__ import annotations

import time

from runi_moodle_cli.client import call
from runi_moodle_cli.formatting import fmt_date, html_to_text, unescape_text

DAY = 86400
PAGE_SIZE = 50
MAX_PAGES = 10


def upcoming_deadlines(days: int = 14, include_overdue: bool = False, year: int | None = None) -> list[dict]:
    if days < 1:
        raise ValueError("days must be at least 1")
    now = int(time.time())
    timesortfrom = now - 30 * DAY if include_overdue else now
    timesortto = now + days * DAY

    events: list[dict] = []
    after_id: int | None = None
    for _ in range(MAX_PAGES):
        params: dict[str, int] = {
            "timesortfrom": timesortfrom,
            "timesortto": timesortto,
            "limitnum": PAGE_SIZE,
        }
        if after_id is not None:
            params["aftereventid"] = after_id
        res = call("core_calendar_get_action_events_by_timesort", year=year, **params)
        batch = res.get("events", [])
        events.extend(batch)
        last_id = res.get("lastid")
        if len(batch) < PAGE_SIZE or not last_id or last_id == after_id:
            break
        after_id = int(last_id)

    return [
        {
            "name": unescape_text(e.get("activityname") or e.get("name") or ""),
            "type": e.get("modulename"),
            "course": html_to_text((e.get("course") or {}).get("fullname", "")),
            "due": fmt_date(int(e.get("timesort") or 0)),
            "overdue": bool(e.get("overdue")),
            "action": unescape_text((e.get("action") or {}).get("name")) or None,
            "url": e.get("url"),
        }
        for e in events
    ]
