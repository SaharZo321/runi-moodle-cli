"""Courses, course staff, course contents, and fuzzy course resolution."""

from __future__ import annotations

import re

from moodle_cli.client import call
from moodle_cli.config import get_site
from moodle_cli.formatting import file_list, html_to_text, unescape_text

# RUNI short names look like "Computer Networks-592-2026/1 - 2601137":
# <name>-<course number>-<year>/<semester> - <moodle id>
SHORTNAME = re.compile(r"-(\d+)-\d{4}/(\d+)\s*-\s*\d+$")

STAFF_ROLES = {
    "editingteacher": "lecturer",
    "teacher": "lecturer",
    "noneditingteacher": "TA",
    "tutor": "TA",
    "instruction_assistant": "TA",
}


def list_courses(semester: int | None = None, year: int | None = None) -> list[dict]:
    site = get_site(year)
    payload = call(
        "core_course_get_enrolled_courses_by_timeline_classification",
        year=year,
        classification="all",
    )
    raw_courses = payload.get("courses", []) if isinstance(payload, dict) else payload
    courses = []
    for c in raw_courses:
        match = SHORTNAME.search(c.get("shortname", ""))
        course = {
            "id": c["id"],
            "name": unescape_text(c.get("fullnamedisplay") or c.get("fullname") or ""),
            "course_number": int(match.group(1)) if match else None,
            "semester": int(match.group(2)) if match else None,
            "url": f"{site.url}/course/view.php?id={c['id']}",
        }
        if semester is None or course["semester"] == semester:
            courses.append(course)
    courses.sort(key=lambda x: (x["semester"] or 99, x["name"]))
    return courses


def resolve_course(query: str, year: int | None = None) -> dict:
    """Resolve a course by Moodle ID, RUNI course number, or partial name."""
    raw = query.strip()
    if not raw:
        raise ValueError("Course query cannot be empty.")

    courses = list_courses(year=year)
    if raw.isdigit():
        num = int(raw)
        by_id = [c for c in courses if c["id"] == num]
        if by_id:
            return by_id[0]
        by_num = [c for c in courses if c["course_number"] == num]
        if len(by_num) == 1:
            return by_num[0]
        if len(by_num) > 1:
            choices = ", ".join(f"{c['id']} ({c['name']})" for c in by_num)
            raise ValueError(f"Course number {num} matched multiple courses: {choices}. Pass the Moodle id instead.")

    lower = raw.lower()
    exact = [c for c in courses if c["name"].lower() == lower]
    if len(exact) == 1:
        return exact[0]

    matches = [c for c in courses if lower in c["name"].lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        choices = "\n".join(
            f"  - {c['id']}: {c['name']}" + (f" (#{c['course_number']})" if c["course_number"] else "")
            for c in matches
        )
        raise ValueError(f"Multiple courses match {query!r}:\n{choices}\nPass a more specific name or the course id.")

    raise ValueError(f"No course matching {query!r}. Run `mdl courses` to see your enrolled courses.")


def get_course_staff(course_id: int, year: int | None = None) -> list[dict]:
    users = call(
        "core_enrol_get_enrolled_users",
        year=year,
        courseid=course_id,
        **{"options[0][name]": "userfields", "options[0][value]": "id,fullname,roles"},
    )
    staff = []
    for u in users:
        roles = {STAFF_ROLES[r["shortname"]] for r in u.get("roles", []) if r["shortname"] in STAFF_ROLES}
        if roles:
            staff.append({"id": u["id"], "name": u["fullname"], "role": "lecturer" if "lecturer" in roles else "TA"})
    staff.sort(key=lambda s: (s["role"] != "lecturer", s["name"]))
    return staff


def get_course_contents(course_id: int, year: int | None = None) -> list[dict]:
    sections = []
    for s in call("core_course_get_contents", year=year, courseid=course_id):
        items = []
        for m in s["modules"]:
            if not m.get("uservisible", True):
                continue
            item = {
                "id": m["id"],
                "type": m.get("modname") or "item",
                "name": unescape_text(m.get("name") or ""),
                "url": m.get("url"),
            }
            if description := html_to_text(m.get("description", "")):
                item["description"] = description
            if m.get("modname") == "url":
                item["link"] = next((c["fileurl"] for c in m.get("contents", []) if c.get("fileurl")), None)
            elif files := file_list([{"files": m.get("contents", [])}]):
                item["files"] = files
            items.append(item)
        summary = html_to_text(s.get("summary", ""))
        if items or summary:
            sections.append(
                {
                    "section": unescape_text(s.get("name") or ""),
                    **({"summary": summary} if summary else {}),
                    "items": items,
                }
            )
    return sections
