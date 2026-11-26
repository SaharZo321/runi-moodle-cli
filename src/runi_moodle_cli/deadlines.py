"""Upcoming actionable deadlines from Moodle's timeline."""

from __future__ import annotations

import time

from runi_moodle_cli.client import call
from runi_moodle_cli.formatting import fmt_date, html_to_text

DAY = 86400


def upcoming_deadlines(days: int = 14, include_overdue: bool = False, year: int | None = None) -> list[dict]:
    if days < 1:
        raise ValueError("days must be at least 1")
    now = int(time.time())
    events = call(
        "core_calendar_get_action_events_by_timesort",
        year=year,
        timesortfrom=now - 30 * DAY if include_overdue else now,
        timesortto=now + days * DAY,
        limitnum=50,
    )["events"]
    return [
        {
            "name": e.get("activityname") or e["name"],
            "type": e.get("modulename"),
            "course": html_to_text(e["course"]["fullname"]),
            "due": fmt_date(e["timesort"]),
            "overdue": bool(e.get("overdue")),
            "action": (e.get("action") or {}).get("name"),
            "url": e.get("url"),
        }
        for e in events
    ]
