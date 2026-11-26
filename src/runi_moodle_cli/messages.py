"""Lecturer and TA announcements, forum replies, and direct messages."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

from runi_moodle_cli.client import call, get_user_id
from runi_moodle_cli.courses import get_course_staff, list_courses
from runi_moodle_cli.formatting import fmt_date, html_to_text

MAX_TEXT = 2000


def _short(raw: str, limit: int = MAX_TEXT) -> str:
    text = html_to_text(raw)
    return text if len(text) <= limit else text[:limit] + " ..."


def _active_discussions(forum: dict, since: float, year: int | None) -> list[dict]:
    active, page = [], 0
    while True:
        batch = call(
            "mod_forum_get_forum_discussions",
            year=year,
            forumid=forum["id"],
            page=page,
            perpage=10,
        )["discussions"]
        active += [d for d in batch if d["timemodified"] >= since]
        if len(batch) < 10 or any(d["timemodified"] < since and not d["pinned"] for d in batch):
            return active
        page += 1


def _staff_posts(
    forum: dict,
    d: dict,
    course: dict,
    staff: dict[int, dict],
    since: float,
    year: int | None,
) -> list[dict]:
    if forum["type"] == "news" and d["created"] >= since and d["numreplies"] == 0:
        return [
            {
                "kind": "announcement",
                "course": course["name"],
                "forum": forum["name"],
                "subject": d["subject"],
                "author": d["userfullname"],
                "role": staff.get(d["userid"], {}).get("role"),
                "time": fmt_date(d["created"]),
                "text": _short(d["message"]),
                "url": f"{course['url'].split('/course/')[0]}/mod/forum/discuss.php?d={d['discussion']}",
            }
        ]
    posts = call("mod_forum_get_discussion_posts", year=year, discussionid=d["discussion"])["posts"]
    by_id = {p["id"]: p for p in posts}
    found = []
    for p in posts:
        if p["author"]["id"] in staff and p["timecreated"] >= since:
            parent = by_id.get(p["parentid"])
            kind = "forum_reply" if parent else ("announcement" if forum["type"] == "news" else "forum_post")
            found.append(
                {
                    "kind": kind,
                    "course": course["name"],
                    "forum": forum["name"],
                    "subject": p["subject"],
                    "author": p["author"]["fullname"],
                    "role": staff[p["author"]["id"]]["role"],
                    "time": fmt_date(p["timecreated"]),
                    "text": _short(p["message"]),
                    **({"in_reply_to": _short(parent["message"], 300)} if parent else {}),
                    "url": p["urls"]["view"],
                }
            )
    return found


def _direct_messages(since: float, year: int | None) -> list[dict]:
    me = get_user_id(year)
    found = []
    for conv in call("core_message_get_conversations", year=year, userid=me, limitnum=50)["conversations"]:
        others = {m["id"]: m["fullname"] for m in conv["members"] if m["id"] != me}
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
            if m["useridfrom"] != me:
                found.append(
                    {
                        "kind": "direct_message",
                        "author": others.get(m["useridfrom"]),
                        "time": fmt_date(m["timecreated"]),
                        "text": _short(m["text"]),
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
        staff = dict(zip(busy, pool.map(lambda c: {s["id"]: s for s in get_course_staff(c, year)}, busy)))
        results = pool.map(
            lambda fd: _staff_posts(fd[0], fd[1], courses[fd[0]["course"]], staff[fd[0]["course"]], since, year),
            active,
        )
        found = [p for posts in results for p in posts]

    if course_id is None:
        found += _direct_messages(since, year)
    found.sort(key=lambda x: x["time"] or "", reverse=True)
    return found
