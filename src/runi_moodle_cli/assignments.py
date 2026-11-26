"""Assignments listing, detail view, and new/updated assignment detection."""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

from runi_moodle_cli.client import call
from runi_moodle_cli.config import data_dir, get_site
from runi_moodle_cli.formatting import file_list, fmt_date, html_to_text

STATUSES = ("all", "pending", "submitted")


def _submission_status(assign_id: int, team: bool, year: int | None) -> str:
    result = call("mod_assign_get_submission_status", year=year, assignid=assign_id)
    attempt = result.get("lastattempt") or {}
    submission = attempt.get("teamsubmission") if team else attempt.get("submission")
    return (submission or {}).get("status", "new")


def list_assignments(status: str = "all", include_overdue: bool = False, year: int | None = None) -> list[dict]:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}, got {status!r}")
    site = get_site(year)
    result = call("mod_assign_get_assignments", year=year)
    now = time.time()
    assignments = []
    for course in result["courses"]:
        for a in course["assignments"]:
            if not include_overdue and a["duedate"] and a["duedate"] < now:
                continue
            assignments.append(
                {
                    "id": a["id"],
                    "name": a["name"],
                    "course": course["fullname"],
                    "due": fmt_date(a["duedate"]),
                    "cutoff": fmt_date(a["cutoffdate"]),
                    "url": f"{site.url}/mod/assign/view.php?id={a['cmid']}",
                    "timemodified": a["timemodified"],
                    "_team": bool(a["teamsubmission"]),
                }
            )

    if status != "all":
        with ThreadPoolExecutor(max_workers=8) as pool:
            statuses = pool.map(lambda a: _submission_status(a["id"], a["_team"], year), assignments)
            for a, s in zip(assignments, list(statuses)):
                a["status"] = s
        wanted = {"submitted"} if status == "submitted" else {"new", "draft"}
        assignments = [a for a in assignments if a["status"] in wanted]

    for a in assignments:
        del a["_team"]
    assignments.sort(key=lambda x: x["due"] or "9999")
    return assignments


def get_assignment(assignment_id: int, year: int | None = None) -> dict:
    site = get_site(year)
    for course in call("mod_assign_get_assignments", year=year)["courses"]:
        a = next((x for x in course["assignments"] if x["id"] == assignment_id), None)
        if a:
            break
    else:
        raise ValueError(f"No assignment with id {assignment_id} in {site.url}")

    status = call("mod_assign_get_submission_status", year=year, assignid=assignment_id)
    attempt = status.get("lastattempt") or {}
    submission = (attempt.get("teamsubmission") if a["teamsubmission"] else attempt.get("submission")) or {}
    feedback = status.get("feedback") or {}
    feedback_plugins = feedback.get("plugins", [])

    return {
        "id": a["id"],
        "name": a["name"],
        "course": course["fullname"],
        "due": fmt_date(a["duedate"]),
        "cutoff": fmt_date(a["cutoffdate"]),
        "url": f"{site.url}/mod/assign/view.php?id={a['cmid']}",
        "description": html_to_text(a["intro"]),
        "attachments": file_list([{"files": a.get("introattachments", [])}]),
        "submission": {
            "status": submission.get("status", "new"),
            "last_modified": fmt_date(submission.get("timemodified", 0)),
            "files": file_list([fa for p in submission.get("plugins", []) for fa in p.get("fileareas", [])]),
            "grading_status": attempt.get("gradingstatus"),
        },
        "grade": html_to_text(feedback["gradefordisplay"]) if feedback.get("gradefordisplay") else None,
        "feedback_comments": "\n\n".join(
            t for p in feedback_plugins for e in p.get("editorfields", []) if (t := html_to_text(e.get("text", "")))
        )
        or None,
        "feedback_files": file_list([fa for p in feedback_plugins for fa in p.get("fileareas", [])]),
    }


def new_assignments(year: int | None = None) -> dict:
    site = get_site(year)
    state_dir = data_dir() / "state"
    state_file = state_dir / f"seen_assignments_{site.year}.json"
    first_run = not state_file.exists()
    seen = {} if first_run else json.loads(state_file.read_text(encoding="utf-8"))

    now = time.time()
    midnight = datetime.combine(date.today(), datetime.min.time()).timestamp()
    new, updated = [], []
    for a in list_assignments(include_overdue=True, year=site.year):
        key = str(a["id"])
        if key not in seen:
            seen[key] = {"first_seen": 0 if first_run else now}
        if seen[key]["first_seen"] >= midnight:
            new.append(a)
        elif a["timemodified"] >= midnight:
            updated.append(a)

    state_dir.mkdir(parents=True, exist_ok=True)
    state_file.write_text(json.dumps(seen, indent=2) + "\n", encoding="utf-8")
    return {"first_run": first_run, "new": new, "updated": updated}
