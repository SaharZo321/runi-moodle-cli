"""Assignments listing, detail view, and new/updated assignment detection."""

from __future__ import annotations

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

from moodle_cli.client import call_site
from moodle_cli.config import Site, atomic_write_text, config_path, data_dir, get_site
from moodle_cli.formatting import file_list, fmt_date, html_to_text, unescape_text

STATUSES = ("all", "pending", "submitted")


def _submission_status(site: Site, assign_id: int, team: bool) -> str:
    result = call_site(site, "mod_assign_get_submission_status", assignid=assign_id)
    attempt = result.get("lastattempt") or {}
    submission = attempt.get("teamsubmission") if team else attempt.get("submission")
    return (submission or {}).get("status", "new")


def list_assignments(status: str = "all", include_overdue: bool = False, year: int | None = None) -> list[dict]:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}, got {status!r}")
    site = get_site(year)
    result = call_site(site, "mod_assign_get_assignments")
    now = time.time()
    assignments = []
    for course in result.get("courses", []):
        course_name = unescape_text(course.get("fullname") or "")
        for a in course.get("assignments", []):
            duedate = int(a.get("duedate") or 0)
            if not include_overdue and duedate and duedate < now:
                continue
            assignments.append(
                {
                    "id": a["id"],
                    "name": unescape_text(a.get("name") or ""),
                    "course": course_name,
                    "due": fmt_date(duedate),
                    "cutoff": fmt_date(int(a.get("cutoffdate") or 0)),
                    "url": f"{site.url}/mod/assign/view.php?id={a['cmid']}",
                    "timemodified": int(a.get("timemodified") or 0),
                    "_team": bool(a.get("teamsubmission")),
                }
            )

    if status != "all" and assignments:
        with ThreadPoolExecutor(max_workers=8) as pool:
            statuses = pool.map(lambda a: _submission_status(site, a["id"], a["_team"]), assignments)
            for a, s in zip(assignments, list(statuses)):
                a["status"] = s
        wanted = {"submitted"} if status == "submitted" else {"new", "draft", "reopened"}
        assignments = [a for a in assignments if a["status"] in wanted]

    for a in assignments:
        del a["_team"]
    assignments.sort(key=lambda x: x["due"] or "9999")
    return assignments


def get_assignment(assignment_id: int, year: int | None = None) -> dict:
    site = get_site(year)
    for course in call_site(site, "mod_assign_get_assignments").get("courses", []):
        a = next((x for x in course.get("assignments", []) if x["id"] == assignment_id), None)
        if a:
            break
    else:
        raise ValueError(f"No assignment with id {assignment_id} in {site.url}")

    status = call_site(site, "mod_assign_get_submission_status", assignid=assignment_id)
    attempt = status.get("lastattempt") or {}
    submission = (attempt.get("teamsubmission") if a.get("teamsubmission") else attempt.get("submission")) or {}
    feedback = status.get("feedback") or {}
    feedback_plugins = feedback.get("plugins", [])

    return {
        "id": a["id"],
        "name": unescape_text(a.get("name") or ""),
        "course": unescape_text(course.get("fullname") or ""),
        "due": fmt_date(int(a.get("duedate") or 0)),
        "cutoff": fmt_date(int(a.get("cutoffdate") or 0)),
        "url": f"{site.url}/mod/assign/view.php?id={a['cmid']}",
        "description": html_to_text(a.get("intro") or ""),
        "attachments": file_list([{"files": a.get("introattachments", [])}]),
        "submission": {
            "status": submission.get("status", "new"),
            "last_modified": fmt_date(int(submission.get("timemodified") or 0)),
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
    state_key = f"{config_path().resolve()}\0{site.url}"
    site_hash = hashlib.sha256(state_key.encode("utf-8")).hexdigest()[:12]
    state_file = state_dir / f"seen_assignments_{site.year}_{site_hash}.json"
    first_run = not state_file.exists()
    seen: dict = {}
    if not first_run:
        try:
            loaded = json.loads(state_file.read_text(encoding="utf-8"))
            seen = loaded if isinstance(loaded, dict) else {}
        except (json.JSONDecodeError, OSError):
            seen = {}
            first_run = True

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

    atomic_write_text(state_file, json.dumps(seen, indent=2) + "\n")
    return {"first_run": first_run, "new": new, "updated": updated}
