"""Tests for Moodle modules: course resolution, reading, and file downloads."""

from __future__ import annotations

import io
import json
import os
import time

import httpx
import pytest
from docx import Document
from pptx import Presentation
from pypdf import PdfWriter

from moodle_cli import (
    assignments,
    calendar,
    client,
    courses,
    deadlines,
    formatting,
    grades,
    messages,
    reading,
    search,
)
from moodle_cli.config import AuthError


def _mock_courses(moodle_api):
    def handler(request: httpx.Request):
        body = request.content.decode()
        if "core_webservice_get_site_info" in body:
            return httpx.Response(200, json={"userid": 10})
        if "core_course_get_enrolled_courses_by_timeline_classification" in body:
            return httpx.Response(
                200,
                json={
                    "courses": [
                        {"id": 101, "fullname": "Data Structures", "shortname": "Data Structures-59-2027/1 - 27001"},
                        {"id": 102, "fullname": "Computer Networks", "shortname": "Computer Networks-592-2027/2 - 27002"},
                    ]
                },
            )
        if "core_course_get_contents" in body:
            return httpx.Response(
                200,
                json=[
                    {
                        "name": "Week 1: Intro",
                        "summary": "",
                        "modules": [
                            {
                                "id": 500,
                                "modname": "resource",
                                "name": "Lecture 1",
                                "url": "https://moodle.runi.ac.il/2027/mod/resource/view.php?id=500",
                                "contents": [
                                    {
                                        "filename": "lecture1.txt",
                                        "fileurl": "https://moodle.runi.ac.il/2027/webservice/pluginfile.php/1/mod_resource/content/1/lecture1.txt",
                                    }
                                ],
                            }
                        ],
                    }
                ],
            )
        return httpx.Response(404)

    moodle_api.post("https://moodle.runi.ac.il/2027/webservice/rest/server.php").mock(side_effect=handler)


def test_resolve_course_by_id_number_and_name(configured_site, moodle_api):
    _mock_courses(moodle_api)
    assert courses.resolve_course("101")["name"] == "Data Structures"
    assert courses.resolve_course("592")["id"] == 102
    assert courses.resolve_course("networks")["id"] == 102

    with pytest.raises(ValueError, match="No course matching"):
        courses.resolve_course("Quantum Physics")


def test_download_file_and_course_files(configured_site, moodle_api, tmp_path):
    _mock_courses(moodle_api)
    file_url = "https://moodle.runi.ac.il/2027/pluginfile.php/1/mod_resource/content/1/lecture1.txt"
    ws_url = "https://moodle.runi.ac.il/2027/webservice/pluginfile.php/1/mod_resource/content/1/lecture1.txt"
    moodle_api.get(ws_url).mock(
        return_value=httpx.Response(200, content=b"Hello lecture 1", headers={"content-type": "text/plain"})
    )

    out_file = tmp_path / "custom.txt"
    res = reading.download_file(file_url, dest=out_file)
    assert res["skipped"] is False
    assert res["bytes"] == 15
    assert out_file.read_bytes() == b"Hello lecture 1"

    # Second call without overwrite skips
    res2 = reading.download_file(file_url, dest=out_file)
    assert res2["skipped"] is True

    # Download entire course
    course_dir = tmp_path / "course_dl"
    summary = reading.download_course_files(101, dest_dir=course_dir)
    assert len(summary["downloaded"]) == 1
    assert not summary["errors"]
    saved = course_dir / "Week 1_ Intro" / "lecture1.txt"
    assert saved.read_bytes() == b"Hello lecture 1"


def test_download_rejects_foreign_host(configured_site):
    with pytest.raises(ValueError, match="configured Moodle site"):
        reading.download_file("https://evil.example.com/pluginfile.php/1/secret.pdf")


def test_formatting_sanitize_html_and_file_list():
    assert formatting.sanitize_filename("CON.pdf") == "_CON.pdf"
    assert formatting.sanitize_filename("nul") == "_nul"
    assert formatting.sanitize_filename("normal\u202ename.pdf") == "normalname.pdf"
    long_heb = "א" * 150 + ".pdf"
    sanitized = formatting.sanitize_filename(long_heb)
    assert len(sanitized.encode("utf-8")) <= formatting.MAX_FILENAME_BYTES
    assert sanitized.endswith(".pdf")

    html_in = "<style>body{color:red}</style><script>alert(1)</script><p>Hello &amp; <b>world</b></p>"
    assert formatting.html_to_text(html_in) == "Hello & world"

    # Missing fileurl or filename is safely skipped
    files = formatting.file_list(
        [
            {
                "files": [
                    {"filename": "ok.pdf", "fileurl": "https://moodle.runi.ac.il/2027/webservice/pluginfile.php/1/ok.pdf"},
                    {"filename": "structure"},
                    {"fileurl": "https://moodle.runi.ac.il/2027/pluginfile.php/1/noname"},
                ]
            }
        ]
    )
    assert files == [{"filename": "ok.pdf", "url": "https://moodle.runi.ac.il/2027/pluginfile.php/1/ok.pdf"}]


def test_client_500_retry_non_json_and_error_codes(configured_site, moodle_api, monkeypatch):
    monkeypatch.setattr("time.sleep", lambda _: None)
    calls = {"n": 0}

    def retry_handler(request: httpx.Request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, text="Service Unavailable")
        return httpx.Response(200, json={"userid": 7})

    moodle_api.post("https://moodle.runi.ac.il/2027/webservice/rest/server.php").mock(side_effect=retry_handler)
    assert client.call("core_webservice_get_site_info")["userid"] == 7
    assert calls["n"] == 2

    moodle_api.post("https://moodle.runi.ac.il/2027/webservice/rest/server.php").mock(
        return_value=httpx.Response(200, text="<html>Maintenance</html>")
    )
    with pytest.raises(client.MoodleError, match="non-JSON"):
        client.call("core_webservice_get_site_info")

    moodle_api.post("https://moodle.runi.ac.il/2027/webservice/rest/server.php").mock(
        return_value=httpx.Response(200, json={"exception": "moodle_exception", "errorcode": "invalidtoken", "message": "Invalid token"})
    )
    with pytest.raises(AuthError, match="invalidtoken"):
        client.call("core_webservice_get_site_info")

    moodle_api.post("https://moodle.runi.ac.il/2027/webservice/rest/server.php").mock(
        return_value=httpx.Response(200, json={"exception": "moodle_exception", "errorcode": "accessexception", "message": "No access"})
    )
    with pytest.raises(client.MoodleError, match="accessexception"):
        client.call("core_enrol_get_enrolled_users")


def test_reading_formats_security_and_error_redaction(configured_site, moodle_api, tmp_path):
    base_file = "https://moodle.runi.ac.il/2027/pluginfile.php/1/mod_resource/content/1"
    ws_base = "https://moodle.runi.ac.il/2027/webservice/pluginfile.php/1/mod_resource/content/1"

    # Negative offset rejected
    with pytest.raises(ValueError, match="offset"):
        reading.read_content(f"{base_file}/a.txt", offset=-1)

    # Path traversal rejected
    with pytest.raises(ValueError, match="configured Moodle site"):
        reading.read_content("https://moodle.runi.ac.il/2027/../2026/pluginfile.php/1/a.txt")

    # Page URL without ?id= rejected cleanly
    with pytest.raises(ValueError, match="numeric"):
        reading.read_content("https://moodle.runi.ac.il/2027/mod/page/view.php")

    # Cross-origin redirect rejected without following
    evil = moodle_api.get("https://evil.example/steal").mock(return_value=httpx.Response(200, content=b"evil"))
    moodle_api.get(f"{ws_base}/redir.txt").mock(
        return_value=httpx.Response(302, headers={"location": "https://evil.example/steal"})
    )
    with pytest.raises(ValueError, match="cross-origin"):
        reading.read_content(f"{base_file}/redir.txt")
    assert evil.call_count == 0

    # HTTP 404 error must NEVER leak wstoken
    moodle_api.get(f"{ws_base}/missing.txt").mock(return_value=httpx.Response(404))
    with pytest.raises(client.MoodleError) as exc_info:
        reading.read_content(f"{base_file}/missing.txt")
    assert "tok2027secretvalue" not in str(exc_info.value)

    # Streaming size cap aborts before reading past MAX_READ_BYTES
    moodle_api.get(f"{ws_base}/huge.txt").mock(
        return_value=httpx.Response(200, content=b"x" * 10, headers={"content-type": "text/plain", "content-length": str(reading.MAX_READ_BYTES + 1)})
    )
    with pytest.raises(ValueError, match="25 MB"):
        reading.read_content(f"{base_file}/huge.txt")

    # PDF, DOCX, PPTX, and ipynb extraction
    pdf_buf = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.write(pdf_buf)
    moodle_api.get(f"{ws_base}/sample.pdf").mock(
        return_value=httpx.Response(200, content=pdf_buf.getvalue(), headers={"content-type": "application/pdf"})
    )
    assert "--- page 1 ---" in reading.read_content(f"{base_file}/sample.pdf")["text"]

    docx_buf = io.BytesIO()
    doc = Document()
    doc.add_paragraph("Hello DOCX world")
    doc.save(docx_buf)
    moodle_api.get(f"{ws_base}/sample.docx").mock(
        return_value=httpx.Response(200, content=docx_buf.getvalue(), headers={"content-type": reading.DOCX})
    )
    assert "Hello DOCX world" in reading.read_content(f"{base_file}/sample.docx")["text"]

    pptx_buf = io.BytesIO()
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    tb = slide.shapes.add_textbox(0, 0, 1000000, 1000000)
    tb.text_frame.text = "Hello PPTX slide"
    prs.save(pptx_buf)
    moodle_api.get(f"{ws_base}/sample.pptx").mock(
        return_value=httpx.Response(200, content=pptx_buf.getvalue(), headers={"content-type": reading.PPTX})
    )
    assert "Hello PPTX slide" in reading.read_content(f"{base_file}/sample.pptx")["text"]

    nb_bytes = json.dumps({"cells": [{"cell_type": "code", "source": ["print(42)"]}]}).encode()
    moodle_api.get(f"{ws_base}/lab.ipynb").mock(
        return_value=httpx.Response(200, content=nb_bytes, headers={"content-type": "application/json"})
    )
    assert "print(42)" in reading.read_content(f"{base_file}/lab.ipynb")["text"]

    # Symlink destination refused
    if hasattr(os, "symlink"):
        real = tmp_path / "real.txt"
        real.write_text("orig", encoding="utf-8")
        link = tmp_path / "link.txt"
        link.symlink_to(real)
        with pytest.raises(ValueError, match="symlink"):
            reading.download_file(f"{base_file}/sample.pdf", dest=link, overwrite=True)


def test_download_course_deduplicates_filenames(configured_site, moodle_api, tmp_path):
    def handler(request: httpx.Request):
        body = request.content.decode()
        if "core_course_get_contents" in body:
            return httpx.Response(
                200,
                json=[
                    {
                        "name": "Week 1",
                        "summary": "",
                        "modules": [
                            {
                                "id": 1,
                                "modname": "resource",
                                "name": "Part A",
                                "contents": [{"filename": "slides.pdf", "fileurl": "https://moodle.runi.ac.il/2027/webservice/pluginfile.php/1/a/slides.pdf"}],
                            },
                            {
                                "id": 2,
                                "modname": "resource",
                                "name": "Part B",
                                "contents": [{"filename": "slides.pdf", "fileurl": "https://moodle.runi.ac.il/2027/webservice/pluginfile.php/1/b/slides.pdf"}],
                            },
                        ],
                    }
                ],
            )
        return httpx.Response(404)

    moodle_api.post("https://moodle.runi.ac.il/2027/webservice/rest/server.php").mock(side_effect=handler)
    moodle_api.get("https://moodle.runi.ac.il/2027/webservice/pluginfile.php/1/a/slides.pdf").mock(
        return_value=httpx.Response(200, content=b"AAA", headers={"content-type": "application/pdf"})
    )
    moodle_api.get("https://moodle.runi.ac.il/2027/webservice/pluginfile.php/1/b/slides.pdf").mock(
        return_value=httpx.Response(200, content=b"BBB", headers={"content-type": "application/pdf"})
    )

    res = reading.download_course_files(101, dest_dir=tmp_path)
    assert len(res["downloaded"]) == 2
    assert (tmp_path / "Week 1" / "slides.pdf").read_bytes() == b"AAA"
    assert (tmp_path / "Week 1" / "slides (2).pdf").read_bytes() == b"BBB"


def test_unique_target_handles_long_colliding_utf8_names_and_atomic_no_overwrite(tmp_path, monkeypatch):
    # Two colliding >200-byte Hebrew filenames must terminate immediately and fit under 200 bytes
    used: set[tuple] = set()
    p1 = reading._unique_target(tmp_path, "א" * 120 + " 1.pdf", used)
    p2 = reading._unique_target(tmp_path, "א" * 120 + " 2.pdf", used)
    assert p1 != p2
    assert len(p1.name.encode("utf-8")) <= formatting.MAX_FILENAME_BYTES
    assert len(p2.name.encode("utf-8")) <= formatting.MAX_FILENAME_BYTES
    assert p2.name.endswith(" (2).pdf")

    # _publish_temp_file with overwrite=False must not overwrite even if os.link is unavailable
    existing = tmp_path / "dest.txt"
    existing.write_bytes(b"ORIGINAL")
    tmp_src = tmp_path / "incoming.part"
    tmp_src.write_bytes(b"NEW")
    monkeypatch.setattr(os, "link", lambda s, d: (_ for _ in ()).throw(OSError("no hardlinks")))
    skipped = reading._publish_temp_file(tmp_src, existing, overwrite=False)
    assert skipped is True
    assert existing.read_bytes() == b"ORIGINAL"
    assert not tmp_src.exists()

    # Case-insensitive section directory collision protection (macOS/Windows)
    used_ci: set[tuple[str, str]] = set()
    c1 = reading._unique_target(tmp_path / "Week 1", "slides.pdf", used_ci)
    c2 = reading._unique_target(tmp_path / "week 1", "slides.pdf", used_ci)
    assert c1.name == "slides.pdf" and c2.name == "slides (2).pdf"

    # KeyboardInterrupt during POSIX O_EXCL fallback cleans up the partial destination file
    if os.name != "nt":
        dest_int = tmp_path / "interrupted.bin"
        tmp_int = tmp_path / "interrupted.part"
        tmp_int.write_bytes(b"DATA")
        monkeypatch.setattr("shutil.copyfileobj", lambda src, dst: (_ for _ in ()).throw(KeyboardInterrupt()))
        with pytest.raises(KeyboardInterrupt):
            reading._publish_temp_file(tmp_int, dest_int, overwrite=False)
        assert not dest_int.exists()
        assert not tmp_int.exists()


def test_assignments_deadlines_calendar_grades_messages_search(configured_site, moodle_api):
    future_ts = int(time.time()) + 86400 * 3
    now_ts = int(time.time())

    def handler(request: httpx.Request):
        body = request.content.decode()
        if "core_webservice_get_site_info" in body:
            return httpx.Response(200, json={"userid": 10})
        if "core_course_get_enrolled_courses_by_timeline_classification" in body:
            return httpx.Response(
                200,
                json={"courses": [{"id": 101, "fullname": "Cloud &amp; SE", "shortname": "Cloud-3961-2027/1 - 101"}]},
            )
        if "mod_assign_get_assignments" in body:
            return httpx.Response(
                200,
                json={
                    "courses": [
                        {
                            "id": 101,
                            "fullname": "Cloud &amp; SE",
                            "assignments": [
                                {"id": 50, "cmid": 500, "name": "HW &amp; 1", "duedate": future_ts, "cutoffdate": 0, "timemodified": now_ts, "teamsubmission": 0}
                            ],
                        }
                    ]
                },
            )
        if "mod_assign_get_submission_status" in body:
            return httpx.Response(200, json={"lastattempt": {"submission": {"status": "reopened", "timemodified": 0}}})
        if "core_calendar_get_action_events_by_timesort" in body:
            return httpx.Response(
                200,
                json={"events": [{"id": 1, "name": "HW &amp; 1", "modulename": "assign", "course": None, "timesort": future_ts}], "lastid": 1},
            )
        if "core_calendar_get_calendar_events" in body:
            return httpx.Response(
                200,
                json={"events": [{"name": "Exam &amp; Review", "courseid": 101, "eventtype": "due", "timestart": future_ts, "timeduration": 0}]},
            )
        if "gradereport_user_get_grade_items" in body:
            return httpx.Response(200, json={"usergrades": []})
        if "mod_forum_get_forums_by_courses" in body:
            return httpx.Response(200, json=[{"id": 9, "course": 101, "type": "news", "name": "News", "numdiscussions": 1}])
        if "mod_forum_get_forum_discussions" in body:
            return httpx.Response(200, json={"discussions": [{"discussion": 77, "timemodified": now_ts, "created": now_ts, "numreplies": 1, "pinned": False}]})
        if "core_enrol_get_enrolled_users" in body:
            return httpx.Response(
                200,
                json=[
                    {"id": 99, "fullname": "Prof X", "roles": [{"shortname": "editingteacher"}]},
                    {"id": 100, "fullname": "TA Y", "roles": [{"shortname": "noneditingteacher"}]},
                ],
            )
        if "mod_forum_get_discussion_posts" in body:
            return httpx.Response(
                200,
                json={
                    "posts": [
                        {"id": 1, "parentid": 0, "author": {"id": 99, "fullname": "Prof X"}, "timecreated": None, "subject": "Hidden", "message": "x", "urls": {"view": "https://moodle.runi.ac.il/2027/p/1"}},
                        {"id": 2, "parentid": 0, "author": {"id": 99, "fullname": "Prof X"}, "timecreated": now_ts, "subject": "Update", "message": "<p>Hello</p>", "urls": {"view": "https://moodle.runi.ac.il/2027/p/2"}},
                    ]
                },
            )
        if "core_message_get_conversations" in body:
            return httpx.Response(200, json={"conversations": []})
        if "core_search_get_results" in body:
            return httpx.Response(200, json={"totalcount": 1, "results": [{"title": "HW 1", "componentname": "mod_assign", "areaname": "activity", "coursefullname": "Cloud", "content": "desc"}]})
        return httpx.Response(404)

    moodle_api.post("https://moodle.runi.ac.il/2027/webservice/rest/server.php").mock(side_effect=handler)

    # Assignments: reopened counts as pending, missing intro handled cleanly
    pending = assignments.list_assignments(status="pending")
    assert len(pending) == 1 and pending[0]["name"] == "HW & 1"
    detail = assignments.get_assignment(50)
    assert detail["description"] == ""

    # Deadlines: null course handled cleanly
    dl = deadlines.upcoming_deadlines()
    assert len(dl) == 1 and dl[0]["name"] == "HW & 1" and dl[0]["course"] == ""

    # Calendar
    cal = calendar.calendar_events()
    assert len(cal) == 1 and cal[0]["name"] == "Exam & Review" and cal[0]["course"] == "Cloud & SE"

    # Grades: empty usergrades doesn't crash
    assert grades.get_grades(101) == {"course_id": 101, "total": None, "items": []}

    # Messages: null timecreated skipped, valid staff post returned
    msgs = messages.staff_messages(days=7)
    assert len(msgs) == 1 and msgs[0]["subject"] == "Update"

    # Course staff includes noneditingteacher as TA
    staff = courses.get_course_staff(101)
    assert any(s["name"] == "TA Y" and s["role"] == "TA" for s in staff)

    # Search validation & results
    with pytest.raises(ValueError, match="empty"):
        search.search("   ")
    with pytest.raises(ValueError, match="page"):
        search.search("hw", page=-1)
    assert search.search("hw")["total"] == 1


def test_reading_site_year_prefers_longest_prefix_and_default_year():
    from moodle_cli.config import Config, save_config

    save_config(
        Config(
            base_url="https://moodle.uni.edu",
            default_year=2027,
            tokens={2025: "tok_root_old", 2026: "tok_nested", 2027: "tok_root_new"},
            site_urls={
                2025: "https://moodle.uni.edu",
                2026: "https://moodle.uni.edu/moodle",
                2027: "https://moodle.uni.edu",
            },
        )
    )
    # Nested /moodle/ path beats root / even though 2027 is default_year
    assert reading._site_year("https://moodle.uni.edu/moodle/pluginfile.php/1/a.pdf") == 2026
    # On tied root /, default_year (2027) beats older year (2025)
    assert reading._site_year("https://moodle.uni.edu/pluginfile.php/1/a.pdf") == 2027
    # Explicit preferred_year overrides default_year
    assert reading._site_year("https://moodle.uni.edu/pluginfile.php/1/a.pdf", preferred_year=2025) == 2025


def test_new_assignments_isolates_state_per_site_url(monkeypatch, moodle_api, tmp_path):
    from moodle_cli.config import save_token

    now_ts = int(time.time())
    future_ts = now_ts + 86400 * 3
    assign_payload = {
        "courses": [
            {
                "id": 101,
                "fullname": "CS",
                "assignments": [
                    {"id": 50, "cmid": 500, "name": "HW 1", "duedate": future_ts, "cutoffdate": 0, "timemodified": now_ts, "teamsubmission": 0}
                ],
            }
        ]
    }
    moodle_api.post("https://moodle.alpha.edu/webservice/rest/server.php").mock(
        return_value=httpx.Response(200, json=assign_payload)
    )
    moodle_api.post("https://moodle.beta.edu/webservice/rest/server.php").mock(
        return_value=httpx.Response(200, json=assign_payload)
    )

    monkeypatch.setenv("MOODLE_CLI_CONFIG", str(tmp_path / "alpha.json"))
    save_token(2027, "tok_alpha", site_url="https://moodle.alpha.edu")
    first_alpha = assignments.new_assignments(year=2027)
    assert first_alpha["first_run"] is True

    # Separate university config sharing the same MOODLE_CLI_DATA_DIR must still get its own first_run baseline
    monkeypatch.setenv("MOODLE_CLI_CONFIG", str(tmp_path / "beta.json"))
    save_token(2027, "tok_beta", site_url="https://moodle.beta.edu")
    first_beta = assignments.new_assignments(year=2027)
    assert first_beta["first_run"] is True

    # Separate account config on the SAME site and year sharing MOODLE_CLI_DATA_DIR must also get its own baseline
    monkeypatch.setenv("MOODLE_CLI_CONFIG", str(tmp_path / "alpha_second_account.json"))
    save_token(2027, "tok_alpha_2", site_url="https://moodle.alpha.edu")
    first_alpha_2 = assignments.new_assignments(year=2027)
    assert first_alpha_2["first_run"] is True
