"""Tests for Moodle modules: course resolution, reading, and file downloads."""

from __future__ import annotations

import httpx
import pytest

from runi_moodle_cli import courses, reading


def _mock_courses(moodle_api):
    def handler(request: httpx.Request):
        body = request.content.decode()
        if "core_webservice_get_site_info" in body:
            return httpx.Response(200, json={"userid": 10})
        if "core_enrol_get_users_courses" in body:
            return httpx.Response(
                200,
                json=[
                    {"id": 101, "fullname": "Data Structures", "shortname": "Data Structures-59-2027/1 - 27001"},
                    {"id": 102, "fullname": "Computer Networks", "shortname": "Computer Networks-592-2027/2 - 27002"},
                ],
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
