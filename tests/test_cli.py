"""End-to-end CLI tests via Typer's CliRunner."""

from __future__ import annotations

import base64
import json

import httpx
from typer.testing import CliRunner

from runi_moodle_cli.cli import app

runner = CliRunner()


def test_version():
    res = runner.invoke(app, ["--version"])
    assert res.exit_code == 0
    assert "runi-moodle-cli" in res.stdout


def test_unauthenticated_due_exits_3():
    res = runner.invoke(app, ["--json", "due"])
    assert res.exit_code == 3
    payload = json.loads(res.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "auth"


def test_auth_login_cookie_and_courses_json(moodle_api):
    b64_tok = base64.b64encode(b"sig:::long_token_xyz").decode()
    moodle_api.get("https://moodle.runi.ac.il/2027/admin/tool/mobile/launch.php").mock(
        return_value=httpx.Response(303, headers={"location": f"moodlemobile://token={b64_tok}"})
    )

    def post_handler(request: httpx.Request):
        body = request.content.decode()
        if "core_webservice_get_site_info" in body:
            return httpx.Response(200, json={"userid": 7, "fullname": "Sahar Student", "username": "sahar"})
        if "core_enrol_get_users_courses" in body:
            return httpx.Response(
                200,
                json=[{"id": 55, "fullname": "Algorithms", "shortname": "Algorithms-101-2027/1 - 27055"}],
            )
        return httpx.Response(404)

    moodle_api.post("https://moodle.runi.ac.il/2027/webservice/rest/server.php").mock(side_effect=post_handler)

    login_res = runner.invoke(
        app,
        ["--json", "auth", "login", "--year", "2027", "--cookie"],
        input="MoodleSession2027=temp_sess_cookie\n",
    )
    assert login_res.exit_code == 0, login_res.stdout
    login_data = json.loads(login_res.stdout)
    assert login_data["ok"] is True
    assert login_data["userid"] == 7

    courses_res = runner.invoke(app, ["--json", "courses"])
    assert courses_res.exit_code == 0, courses_res.stdout
    courses_data = json.loads(courses_res.stdout)
    assert len(courses_data) == 1
    assert courses_data[0]["name"] == "Algorithms"
    assert courses_data[0]["course_number"] == 101
