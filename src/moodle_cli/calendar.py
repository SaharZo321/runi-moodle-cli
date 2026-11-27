"""Upcoming Moodle calendar events."""

from __future__ import annotations

import time

from moodle_cli.client import call
from moodle_cli.config import get_site
from moodle_cli.courses import list_courses
from moodle_cli.formatting import fmt_date, html_to_text, unescape_text

DAY = 86400
MAX_DESCRIPTION = 500


def _short(raw: str) -> str:
    text = html_to_text(raw)
    return text if len(text) <= MAX_DESCRIPTION else text[:MAX_DESCRIPTION] + "..."


def calendar_events(days: int = 30, course_id: int | None = None, year: int | None = None) -> list[dict]:
    if days < 1:
        raise ValueError("days must be at least 1")
    site = get_site(year)
    courses = {c["id"]: c["name"] for c in list_courses(year=year)}
    if course_id is not None and course_id not in courses:
        raise ValueError(f"Course {course_id} is not one of your courses; run `mdl courses` for valid ids.")
    now = int(time.time())
    events = call(
        "core_calendar_get_calendar_events",
        year=year,
        **{"events[courseids]": [course_id] if course_id is not None else list(courses)},
        **{
            "options[userevents]": 1,
            "options[siteevents]": 1,
            "options[timestart]": now,
            "options[timeend]": now + days * DAY,
        },
    ).get("events", [])
    if course_id is not None:
        events = [e for e in events if e.get("courseid") == course_id]
    events.sort(key=lambda e: (e.get("timestart") or 0, e.get("name") or ""))
    result = []
    for e in events:
        timestart = int(e.get("timestart") or 0)
        duration = int(e.get("timeduration") or 0)
        item = {
            "name": unescape_text(e.get("name") or ""),
            "course": courses.get(e.get("courseid")),
            "type": e.get("eventtype") or "",
            "start": fmt_date(timestart),
            "end": fmt_date(timestart + duration) if duration else None,
        }
        if e.get("modulename"):
            item["activity"] = e["modulename"]
        if location := (e.get("location") or "").strip():
            item["location"] = location
        if description := _short(e.get("description", "")):
            item["description"] = description
        item["url"] = f"{site.url}/calendar/view.php?view=day&time={timestart}"
        result.append(item)
    return result
