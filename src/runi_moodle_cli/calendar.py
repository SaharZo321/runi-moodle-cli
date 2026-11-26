"""Upcoming Moodle calendar events."""

from __future__ import annotations

import time

from runi_moodle_cli.client import call, get_user_id
from runi_moodle_cli.config import get_site
from runi_moodle_cli.formatting import fmt_date, html_to_text

DAY = 86400
MAX_DESCRIPTION = 500


def _short(raw: str) -> str:
    text = html_to_text(raw)
    return text if len(text) <= MAX_DESCRIPTION else text[:MAX_DESCRIPTION] + "..."


def calendar_events(days: int = 30, course_id: int | None = None, year: int | None = None) -> list[dict]:
    if days < 1:
        raise ValueError("days must be at least 1")
    site = get_site(year)
    courses = {
        c["id"]: c["fullname"]
        for c in call("core_enrol_get_users_courses", year=year, userid=get_user_id(year))
    }
    if course_id is not None and course_id not in courses:
        raise ValueError(f"Course {course_id} is not one of your courses; run `runi courses` for valid ids.")
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
    )["events"]
    if course_id is not None:
        events = [e for e in events if e.get("courseid") == course_id]
    events.sort(key=lambda e: (e["timestart"], e["name"]))
    result = []
    for e in events:
        item = {
            "name": e["name"],
            "course": courses.get(e.get("courseid")),
            "type": e["eventtype"],
            "start": fmt_date(e["timestart"]),
            "end": fmt_date(e["timestart"] + e["timeduration"]) if e.get("timeduration") else None,
        }
        if e.get("modulename"):
            item["activity"] = e["modulename"]
        if location := (e.get("location") or "").strip():
            item["location"] = location
        if description := _short(e.get("description", "")):
            item["description"] = description
        item["url"] = f"{site.url}/calendar/view.php?view=day&time={e['timestart']}"
        result.append(item)
    return result
