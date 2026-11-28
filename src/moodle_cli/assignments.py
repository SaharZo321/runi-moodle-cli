"""Assignments listing, detail view, and new/updated assignment detection."""

from __future__ import annotations

import hashlib
import html
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path

import httpx

from moodle_cli.client import MoodleError, call_site, redact_secrets, sanitize_http_error, upload_draft_file
from moodle_cli.config import AuthError, Site, atomic_write_text, config_path, data_dir, get_site
from moodle_cli.formatting import browser_url, file_list, fmt_date, html_to_text, unescape_text

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


def _plugin_configs(assignment_raw: dict) -> dict[str, dict[str, str]]:
    configs: dict[str, dict[str, str]] = {}
    for entry in assignment_raw.get("configs") or []:
        if not isinstance(entry, dict) or entry.get("subtype") != "assignsubmission":
            continue
        plugin = str(entry.get("plugin") or "")
        name = str(entry.get("name") or "")
        if plugin and name:
            configs.setdefault(plugin, {})[name] = str(entry.get("value") or "")
    return configs


def _submission_files(submission: dict) -> list[dict]:
    """Extract all submitted file attachments without filtering out image_page* filenames."""
    out: list[dict] = []
    plugins = submission.get("plugins")
    if not isinstance(plugins, list):
        return out
    for plugin in plugins:
        if not isinstance(plugin, dict):
            continue
        fileareas = plugin.get("fileareas")
        if not isinstance(fileareas, list):
            continue
        for fa in fileareas:
            if not isinstance(fa, dict):
                continue
            files = fa.get("files")
            if not isinstance(files, list):
                continue
            for f in files:
                if not isinstance(f, dict):
                    continue
                name = str(f.get("filename") or "")
                if not name:
                    continue
                out.append(
                    {
                        "filename": name,
                        "url": browser_url(str(f.get("fileurl") or "")),
                        "mimetype": f.get("mimetype", ""),
                        "size": f.get("filesize"),
                    }
                )
    return out


def _extract_online_text(submission: dict) -> str:
    parts: list[str] = []
    plugins = submission.get("plugins")
    if not isinstance(plugins, list):
        return ""
    for plugin in plugins:
        if not isinstance(plugin, dict):
            continue
        editorfields = plugin.get("editorfields")
        if not isinstance(editorfields, list):
            continue
        for field in editorfields:
            if isinstance(field, dict) and (text := html_to_text(str(field.get("text") or ""))):
                parts.append(text)
    return "\n\n".join(parts)


def _check_write_warnings(raw: object, wsfunction: str, token: str) -> None:
    """Fail closed if a Moodle write call returns a non-list or any warning."""
    if not isinstance(raw, list):
        raise MoodleError(f"Moodle returned an unexpected response for {wsfunction} (expected warning list).")
    messages: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            messages.append(redact_secrets(str(item), token))
            continue
        code = str(item.get("warningcode") or "").strip()
        msg = str(item.get("message") or code or "Unknown warning").strip()
        combined = f"{code}: {msg}" if code and not msg.lower().startswith(code.lower()) else msg
        messages.append(redact_secrets(combined, token))
    if messages:
        raise MoodleError("; ".join(messages))


def _fetch_assignment_raw(site: Site, assignment_id: int) -> tuple[dict, dict]:
    for course in call_site(site, "mod_assign_get_assignments").get("courses", []):
        a = next((x for x in course.get("assignments", []) if x["id"] == assignment_id), None)
        if a:
            return course, a
    raise ValueError(f"No assignment with id {assignment_id} in {site.url}")


def _state_snapshot(status_str: str, timemodified: int, existing_files: list[dict], existing_text: str) -> dict:
    return {
        "status": status_str,
        "timemodified": timemodified,
        "files": sorted(
            (f["filename"], "" if f.get("size") is None else str(f.get("size"))) for f in existing_files
        ),
        "text_sha256": hashlib.sha256(existing_text.encode("utf-8")).hexdigest() if existing_text else "",
    }


def _verify_submission_contents(
    submission: dict,
    expected_files: list[tuple[str, int]],
    expected_text: str | None,
    *,
    before_finalize: bool,
    assignment_id: int,
) -> list[dict]:
    returned_files = _submission_files(submission)
    if expected_files:
        by_name = {f["filename"].casefold(): f for f in returned_files}
        problems: list[str] = []
        for name, expected_size in expected_files:
            found = by_name.get(name.casefold())
            if found is None:
                problems.append(f"{name} (missing)")
            elif found.get("size") is not None:
                try:
                    actual_size = int(found["size"])
                except (TypeError, ValueError):
                    actual_size = None
                if actual_size is not None and actual_size != expected_size:
                    problems.append(f"{name} (size {actual_size} != expected {expected_size})")
        if problems:
            if before_finalize:
                raise MoodleError(
                    f"Draft was saved, but uploaded file(s) failed verification before finalizing: "
                    f"{', '.join(problems)}. Skipped finalizing so the draft remains editable."
                )
            raise MoodleError(
                f"Moodle saved the submission, but uploaded file(s) failed verification: "
                f"{', '.join(problems)}. Check with `mdl assignment {assignment_id}`."
            )
    if expected_text is not None:
        saved_norm = " ".join(_extract_online_text(submission).split())
        expected_norm = " ".join(expected_text.split())
        if not saved_norm or saved_norm != expected_norm:
            reason = "empty" if not saved_norm else "different from the submitted text"
            if before_finalize:
                raise MoodleError(
                    f"Draft was saved, but online text was {reason} in pre-finalization verification. "
                    "Skipped finalizing so the draft remains editable."
                )
            raise MoodleError(
                f"Moodle saved the submission, but online text was {reason} in post-submit verification. "
                f"Check with `mdl assignment {assignment_id}`."
            )
    return returned_files


def _prepare_submission_context(
    assignment_id: int,
    files: list[Path] | None = None,
    text: str | None = None,
    draft: bool = False,
    finalize_draft: bool = False,
    accept_statement: bool = False,
    year: int | None = None,
) -> tuple[Site, dict, dict, dict]:
    file_paths = list(files or [])
    if finalize_draft:
        if draft:
            raise ValueError("Cannot combine --finalize-draft with --draft.")
        if file_paths or text is not None:
            raise ValueError("Cannot combine --finalize-draft with --file or --text.")
    else:
        if not file_paths and text is None:
            raise ValueError("Provide at least one --file, --text/--text-file, or --finalize-draft.")
        if text is not None and not text.strip():
            raise ValueError("Submission text cannot be empty.")

    site = get_site(year)
    course, a = _fetch_assignment_raw(site, assignment_id)
    status = call_site(site, "mod_assign_get_submission_status", assignid=assignment_id)
    attempt = status.get("lastattempt") if isinstance(status, dict) else None
    if not isinstance(attempt, dict) or not attempt:
        status_warnings = status.get("warnings") if isinstance(status, dict) else None
        if isinstance(status_warnings, list) and status_warnings:
            detail = "; ".join(
                redact_secrets(str(w.get("message") or w.get("warningcode") or w), site.token)
                for w in status_warnings
                if isinstance(w, dict)
            )
            raise ValueError(f"Cannot submit to this assignment: {detail}")
        raise ValueError("Moodle did not return submission status for this assignment (you may not be able to submit).")

    is_team = bool(a.get("teamsubmission"))
    submission = (attempt.get("teamsubmission") if is_team else attempt.get("submission")) or {}
    if not isinstance(submission, dict):
        submission = {}
    current_status = str(submission.get("status") or "new")
    timemodified = int(submission.get("timemodified") or 0)
    existing_files = _submission_files(submission)
    existing_text = _extract_online_text(submission)

    if attempt.get("locked") is True:
        raise ValueError("This assignment submission is locked.")
    if attempt.get("submissionsenabled") is False:
        raise ValueError("No submission methods are enabled for this assignment.")

    submissiondrafts = bool(a.get("submissiondrafts"))
    requires_statement = bool(a.get("requiresubmissionstatement"))
    configs = _plugin_configs(a)

    if finalize_draft:
        if not submissiondrafts:
            raise ValueError("This assignment does not use draft finalization.")
        if current_status != "draft":
            raise ValueError(f"Cannot finalize: current submission status is {current_status!r} (expected 'draft').")
        if attempt.get("cansubmit") is False:
            raise ValueError("Moodle reports this draft cannot currently be submitted for grading.")
        if requires_statement and not accept_statement:
            raise ValueError(
                "This assignment requires accepting the submission statement. Pass --accept-statement to confirm."
            )
        mode = "finalize"
        will_finalize = True
        validated_files: list[dict] = []
    else:
        if attempt.get("canedit") is False:
            raise ValueError(f"This assignment cannot be edited (current status: {current_status}).")
        if draft and not submissiondrafts:
            raise ValueError(
                "This assignment does not support saving drafts; saving immediately submits it for grading. "
                "Omit --draft to submit."
            )
        will_finalize = submissiondrafts and not draft
        if requires_statement and not draft and not accept_statement:
            raise ValueError(
                "This assignment requires accepting the submission statement. Pass --accept-statement to confirm."
            )
        mode = "draft" if draft else "submit"

        validated_files = []
        if file_paths:
            if configs and configs.get("file", {}).get("enabled") != "1":
                raise ValueError("File submissions are disabled for this assignment.")
            seen_names: set[str] = set()
            max_files = int(configs.get("file", {}).get("maxfilesubmissions") or 0)
            max_bytes = int(configs.get("file", {}).get("maxsubmissionsizebytes") or 0)
            if max_files > 0 and len(file_paths) > max_files:
                raise ValueError(
                    f"Too many files: {len(file_paths)} provided, but this assignment allows at most {max_files}."
                )
            for raw_path in file_paths:
                p = Path(raw_path).expanduser()
                if not p.exists():
                    raise ValueError(f"File not found: {raw_path}")
                if not p.is_file():
                    raise ValueError(f"Not a regular file: {raw_path}")
                folded = p.name.casefold()
                if folded in seen_names:
                    raise ValueError(f"Duplicate filename in submission: {p.name!r}")
                seen_names.add(folded)
                size = p.stat().st_size
                if max_bytes > 0 and size > max_bytes:
                    raise ValueError(
                        f"File {p.name!r} is {size} bytes, exceeding the assignment limit of {max_bytes} bytes."
                    )
                validated_files.append({"path": str(p.resolve()), "name": p.name, "size": size})

        if text is not None and configs and configs.get("onlinetext", {}).get("enabled") != "1":
            raise ValueError("Online text submissions are disabled for this assignment.")

    warnings: list[str] = []
    duedate = int(a.get("duedate") or 0)
    if duedate and duedate < time.time():
        warnings.append(f"Assignment due date ({fmt_date(duedate)}) has passed; this will be a late submission.")
    if existing_files and validated_files:
        warnings.append(
            f"Uploading {len(validated_files)} file(s) will replace {len(existing_files)} existing submitted file(s)."
        )
    if existing_text and text is not None:
        warnings.append("Submitting online text will replace your existing online text answer.")
    elif existing_text and validated_files and text is None:
        warnings.append(
            "Existing online text is present; submitting files without --text may clear existing online text on some Moodle versions."
        )
    if any(f["size"] == 0 for f in validated_files):
        warnings.append("One or more files to upload are 0 bytes (empty).")
    if is_team:
        warnings.append("This is a team/group assignment; submitting applies to your entire group.")
    if not draft:
        warnings.append("Submitting for grading may lock the submission from further edits.")

    preview = {
        "assignment_id": a["id"],
        "name": unescape_text(a.get("name") or ""),
        "course": unescape_text(course.get("fullname") or ""),
        "due": fmt_date(duedate),
        "cutoff": fmt_date(int(a.get("cutoffdate") or 0)),
        "url": f"{site.url}/mod/assign/view.php?id={a['cmid']}",
        "current_status": current_status,
        "existing_files": existing_files,
        "existing_text_present": bool(existing_text),
        "files": validated_files,
        "text_length": len(text) if text is not None else None,
        "mode": mode,
        "will_finalize": will_finalize,
        "requires_statement": requires_statement,
        "team_submission": is_team,
        "warnings": warnings,
        "state_snapshot": _state_snapshot(current_status, timemodified, existing_files, existing_text),
    }
    return site, course, a, preview


def prepare_submission(
    assignment_id: int,
    files: list[Path] | None = None,
    text: str | None = None,
    draft: bool = False,
    finalize_draft: bool = False,
    accept_statement: bool = False,
    year: int | None = None,
) -> dict:
    """Validate an assignment submission locally and against Moodle metadata without writing."""
    _, _, _, preview = _prepare_submission_context(
        assignment_id,
        files=files,
        text=text,
        draft=draft,
        finalize_draft=finalize_draft,
        accept_statement=accept_statement,
        year=year,
    )
    return preview


def submit_assignment(
    assignment_id: int,
    files: list[Path] | None = None,
    text: str | None = None,
    draft: bool = False,
    finalize_draft: bool = False,
    accept_statement: bool = False,
    year: int | None = None,
    expected_state: dict | None = None,
) -> dict:
    """Upload files and/or online text to a Moodle assignment, optionally finalizing for grading."""
    site, _, a, preview = _prepare_submission_context(
        assignment_id,
        files=files,
        text=text,
        draft=draft,
        finalize_draft=finalize_draft,
        accept_statement=accept_statement,
        year=year,
    )
    if expected_state is not None and preview["state_snapshot"] != expected_state:
        raise ValueError(
            "Assignment submission state changed while waiting for confirmation; re-run to review the latest state."
        )

    baseline_state = expected_state if expected_state is not None else preview["state_snapshot"]
    result_warnings = list(preview["warnings"])
    saved_draft_this_call = False
    expected_uploaded_files: list[tuple[str, int]] = []

    if not finalize_draft:
        plugindata: dict = {}
        if preview["files"]:
            draft_itemid = 0
            for file_info in preview["files"]:
                uploaded = upload_draft_file(
                    site,
                    Path(file_info["path"]),
                    itemid=draft_itemid,
                    filename=file_info["name"],
                )
                draft_itemid = int(uploaded["itemid"])
                expected_uploaded_files.append(
                    (str(uploaded.get("filename") or file_info["name"]), int(file_info["size"]))
                )
            plugindata["files_filemanager"] = draft_itemid

        if text is not None:
            escaped_html = (
                "<p>" + html.escape(text).replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>") + "</p>"
            )
            plugindata["onlinetext_editor"] = {
                "text": escaped_html,
                "format": 1,
                "itemid": 0,
            }

        if preview["files"]:
            # Recheck remote state right before saving in case the draft changed during upload.
            pre_save = call_site(site, "mod_assign_get_submission_status", assignid=assignment_id)
            pre_attempt = pre_save.get("lastattempt") if isinstance(pre_save, dict) else None
            if not isinstance(pre_attempt, dict) or not pre_attempt:
                raise MoodleError(
                    "Moodle returned an unexpected submission status while rechecking before save."
                )
            pre_sub = (pre_attempt.get("teamsubmission") if a.get("teamsubmission") else pre_attempt.get("submission")) or {}
            if not isinstance(pre_sub, dict):
                raise MoodleError(
                    "Moodle returned an unexpected submission object while rechecking before save."
                )
            live_snap = _state_snapshot(
                str(pre_sub.get("status") or "new"),
                int(pre_sub.get("timemodified") or 0),
                _submission_files(pre_sub),
                _extract_online_text(pre_sub),
            )
            if live_snap != baseline_state:
                raise ValueError(
                    "Assignment submission state changed during file upload; aborted before saving. "
                    "Re-run to review the latest state."
                )

        save_res = call_site(
            site,
            "mod_assign_save_submission",
            retry=False,
            assignmentid=assignment_id,
            plugindata=plugindata,
        )
        _check_write_warnings(save_res, "mod_assign_save_submission", site.token)
        saved_draft_this_call = True

        if preview["will_finalize"] and (expected_uploaded_files or text is not None):
            # Verify uploaded files and online text are present in the saved draft BEFORE finalizing/locking.
            try:
                pre_fin = call_site(site, "mod_assign_get_submission_status", assignid=assignment_id)
            except (AuthError, MoodleError, httpx.HTTPError) as exc:
                detail = sanitize_http_error(exc) if isinstance(exc, httpx.HTTPError) else str(exc)
                msg = (
                    f"Draft was saved to Moodle, but pre-finalization verification failed ({detail}); "
                    f"skipped finalizing. Verify with `mdl assignment {assignment_id}` and retry with `--finalize-draft`."
                )
                if isinstance(exc, AuthError):
                    raise AuthError(msg) from exc
                raise MoodleError(msg) from exc
            pre_fin_attempt = pre_fin.get("lastattempt") if isinstance(pre_fin, dict) else None
            pre_fin_sub = (
                (pre_fin_attempt.get("teamsubmission") if a.get("teamsubmission") else pre_fin_attempt.get("submission"))
                if isinstance(pre_fin_attempt, dict)
                else None
            )
            if not isinstance(pre_fin_sub, dict):
                raise MoodleError(
                    f"Draft was saved to Moodle, but pre-finalization verification returned an unexpected payload; "
                    f"skipped finalizing. Check with `mdl assignment {assignment_id}`."
                )
            _verify_submission_contents(
                pre_fin_sub,
                expected_uploaded_files,
                text,
                before_finalize=True,
                assignment_id=assignment_id,
            )

    if preview["will_finalize"]:
        try:
            submit_res = call_site(
                site,
                "mod_assign_submit_for_grading",
                retry=False,
                assignmentid=assignment_id,
                acceptsubmissionstatement=1 if accept_statement else 0,
            )
            _check_write_warnings(submit_res, "mod_assign_submit_for_grading", site.token)
        except (AuthError, MoodleError, httpx.HTTPError) as exc:
            detail = sanitize_http_error(exc) if isinstance(exc, httpx.HTTPError) else str(exc)
            if saved_draft_this_call:
                msg = (
                    f"Draft was saved to Moodle, but finalizing for grading failed ({detail}). "
                    f"Verify with `mdl assignment {assignment_id}` and retry with `--finalize-draft`."
                )
                if isinstance(exc, AuthError):
                    raise AuthError(msg) from exc
                raise MoodleError(msg) from exc
            raise

    # Re-fetch status so the caller gets the confirmed server-side state.
    try:
        after = call_site(site, "mod_assign_get_submission_status", assignid=assignment_id)
    except (AuthError, MoodleError, httpx.HTTPError) as exc:
        detail = sanitize_http_error(exc) if isinstance(exc, httpx.HTTPError) else str(exc)
        msg = (
            f"Submission was sent to Moodle, but post-submit verification failed ({detail}). "
            f"Check status with `mdl assignment {assignment_id}`."
        )
        if isinstance(exc, AuthError):
            raise AuthError(msg) from exc
        raise MoodleError(msg) from exc

    attempt = after.get("lastattempt") if isinstance(after, dict) else None
    if not isinstance(attempt, dict) or not attempt:
        raise MoodleError(
            f"Submission was sent to Moodle, but post-submit verification returned an unexpected status payload. "
            f"Check status with `mdl assignment {assignment_id}`."
        )
    is_team = bool(a.get("teamsubmission"))
    submission = attempt.get("teamsubmission") if is_team else attempt.get("submission")
    if not isinstance(submission, dict):
        raise MoodleError(
            f"Submission was sent to Moodle, but post-submit verification returned an unexpected submission object. "
            f"Check status with `mdl assignment {assignment_id}`."
        )
    actual_status = str(submission.get("status") or "unknown")
    expected_status = "draft" if draft else "submitted"

    if actual_status != expected_status:
        if is_team and not draft and actual_status == "draft":
            result_warnings.append(
                "Team submission is still in draft; other group members may still need to confirm submission."
            )
        else:
            raise MoodleError(
                f"Moodle returned submission status {actual_status!r} after write (expected {expected_status!r}). "
                f"Check with `mdl assignment {assignment_id}`."
            )

    returned_files = _verify_submission_contents(
        submission,
        expected_uploaded_files,
        text if not finalize_draft else None,
        before_finalize=False,
        assignment_id=assignment_id,
    )

    return {
        "assignment_id": preview["assignment_id"],
        "name": preview["name"],
        "course": preview["course"],
        "url": preview["url"],
        "status": actual_status,
        "last_modified": fmt_date(int(submission.get("timemodified") or 0)),
        "files": returned_files,
        "finalized": actual_status == "submitted",
        "warnings": result_warnings,
    }
