"""Lecturer and TA announcements, forum replies, and direct messages."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

from moodle_cli.client import MoodleError, call, get_user_id
from moodle_cli.courses import get_course_staff, list_courses
from moodle_cli.formatting import fmt_date, html_to_text, unescape_text

MAX_TEXT = 2000
MAX_DISCUSSION_PAGES = 20


def _short(raw: str, limit: int = MAX_TEXT) -> str:
    text = html_to_text(raw)
    return text if len(text) <= limit else text[:limit] + " ..."


def _active_discussions(forum: dict, since: float, year: int | None) -> list[dict]:
    active: list[dict] = []
    for page in range(MAX_DISCUSSION_PAGES):
        batch = call(
            "mod_forum_get_forum_discussions",
            year=year,
            forumid=forum["id"],
            page=page,
            perpage=10,
        ).get("discussions", [])
        active += [d for d in batch if (d.get("timemodified") or 0) >= since]
        if len(batch) < 10 or any((d.get("timemodified") or 0) < since and not d.get("pinned") for d in batch):
            return active
    return active


def _staff_posts(
    forum: dict,
    d: dict,
    course: dict,
    staff: dict[int, dict],
    since: float,
    year: int | None,
) -> list[dict]:
    created = d.get("created") or 0
    if forum.get("type") == "news" and created >= since and d.get("numreplies") == 0:
        return [
            {
                "kind": "announcement",
                "course": course["name"],
                "forum": unescape_text(forum.get("name") or ""),
                "subject": unescape_text(d.get("subject") or ""),
                "author": d.get("userfullname"),
                "role": staff.get(d.get("userid"), {}).get("role"),
                "time": fmt_date(int(created)),
                "text": _short(d.get("message") or ""),
                "url": f"{course['url'].split('/course/')[0]}/mod/forum/discuss.php?d={d['discussion']}",
            }
        ]
    try:
        posts = call("mod_forum_get_discussion_posts", year=year, discussionid=d["discussion"]).get("posts", [])
    except MoodleError:
        return []
    by_id = {p["id"]: p for p in posts if "id" in p}
    found = []
    for p in posts:
        author = p.get("author") or {}
        author_id = author.get("id")
        timecreated = p.get("timecreated")
        if author_id in staff and timecreated is not None and timecreated >= since:
            parent = by_id.get(p.get("parentid"))
            kind = "forum_reply" if parent else ("announcement" if forum.get("type") == "news" else "forum_post")
            found.append(
                {
                    "kind": kind,
                    "course": course["name"],
                    "forum": unescape_text(forum.get("name") or ""),
                    "subject": unescape_text(p.get("subject") or ""),
                    "author": author.get("fullname"),
                    "role": staff[author_id]["role"],
                    "time": fmt_date(int(timecreated)),
                    "text": _short(p.get("message") or ""),
                    **({"in_reply_to": _short(parent.get("message") or "", 300)} if parent else {}),
                    "url": (p.get("urls") or {}).get("view"),
                }
            )
    return found


def _safe_course_staff(course_id: int, year: int | None) -> dict[int, dict]:
    try:
        return {s["id"]: s for s in get_course_staff(course_id, year)}
    except MoodleError:
        return {}


def _direct_messages(since: float, year: int | None) -> list[dict]:
    me = get_user_id(year)
    found = []
    convs = call("core_message_get_conversations", year=year, userid=me, limitnum=50).get("conversations", [])
    for conv in convs:
        others = {m["id"]: m.get("fullname") for m in conv.get("members", []) if m.get("id") != me}
        if not others:
            continue
        msgs = call(
            "core_message_get_conversation_messages",
            year=year,
            currentuserid=me,
            convid=conv["id"],
            newest=1,
            limitnum=50,
            timefrom=int(since),
        )
        for m in msgs.get("messages", []):
            if m.get("useridfrom") != me:
                found.append(
                    {
                        "kind": "direct_message",
                        "author": others.get(m.get("useridfrom")),
                        "time": fmt_date(int(m.get("timecreated") or 0)),
                        "text": _short(m.get("text") or ""),
                    }
                )
    return found


def staff_messages(days: int = 7, course_id: int | None = None, year: int | None = None) -> list[dict]:
    if days < 1:
        raise ValueError("days must be at least 1")
    since = time.time() - days * 86400
    courses = {c["id"]: c for c in list_courses(year=year) if course_id is None or c["id"] == course_id}
    if not courses:
        return []
    forums = call("mod_forum_get_forums_by_courses", year=year, courseids=list(courses))
    forums = [f for f in forums if f.get("numdiscussions", 1)]

    with ThreadPoolExecutor(max_workers=4) as pool:
        active = [
            (f, d)
            for f, ds in zip(forums, pool.map(lambda f: _active_discussions(f, since, year), forums))
            for d in ds
        ]
        busy = sorted({f["course"] for f, _ in active})
        staff = dict(zip(busy, pool.map(lambda c: _safe_course_staff(c, year), busy)))
        results = pool.map(
            lambda fd: _staff_posts(fd[0], fd[1], courses[fd[0]["course"]], staff[fd[0]["course"]], since, year),
            active,
        )
        found = [p for posts in results for p in posts]

    if course_id is None:
        found += _direct_messages(since, year)
    found.sort(key=lambda x: x["time"] or "", reverse=True)
    return found
