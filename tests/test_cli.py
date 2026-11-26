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
        if "core_course_get_enrolled_courses_by_timeline_classification" in body:
            return httpx.Response(
                200,
                json={
                    "courses": [
                        {"id": 55, "fullname": "Algorithms", "shortname": "Algorithms-101-2027/1 - 27055"}
                    ]
                },
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

    # Subcommand-position --json also works
    courses_after = runner.invoke(app, ["courses", "--json"])
    assert courses_after.exit_code == 0, courses_after.stdout
    assert json.loads(courses_after.stdout) == courses_data


def test_read_plain_text_when_piped_and_json_when_requested(monkeypatch):
    monkeypatch.setattr(
        "runi_moodle_cli.reading.read_content",
        lambda url, offset=0: {"name": "notes.pdf", "text": "Hello [world]\nLine 2", "total_chars": 20},
    )

    plain = runner.invoke(app, ["read", "https://moodle.runi.ac.il/2027/pluginfile.php/1/notes.pdf"])
    assert plain.exit_code == 0, plain.stdout
    assert plain.stdout == "Hello [world]\nLine 2\n"

    as_json = runner.invoke(app, ["read", "https://moodle.runi.ac.il/2027/pluginfile.php/1/notes.pdf", "--json"])
    assert as_json.exit_code == 0, as_json.stdout
    payload = json.loads(as_json.stdout)
    assert payload["name"] == "notes.pdf"
    assert payload["text"] == "Hello [world]\nLine 2"


def test_read_tty_escapes_filename_and_preserves_literal_emoji_and_brackets(monkeypatch):
    monkeypatch.setattr(
        "runi_moodle_cli.reading.read_content",
        lambda url, offset=0: {
            "name": "Week [draft].pdf",
            "text": "Status: :warning: [not markup]",
            "total_chars": 30,
        },
    )
    monkeypatch.setattr("runi_moodle_cli.cli._stdout_is_tty", lambda: True)

    res = runner.invoke(app, ["read", "https://moodle.runi.ac.il/2027/pluginfile.php/1/notes.pdf"])
    assert res.exit_code == 0, res.stdout
    assert "Week [draft].pdf" in res.stdout
    assert ":warning:" in res.stdout
    assert "[not markup]" in res.stdout


def test_read_piped_error_is_human_not_json(monkeypatch):
    from runi_moodle_cli.client import MoodleError

    def boom(url, offset=0):
        raise MoodleError("File not found")

    monkeypatch.setattr("runi_moodle_cli.reading.read_content", boom)
    res = runner.invoke(app, ["read", "https://moodle.runi.ac.il/2027/pluginfile.php/1/missing.pdf"])
    assert res.exit_code == 4
    assert "File not found" in res.output
    assert '"ok": false' not in res.stdout


def test_auth_login_default_credentials_and_preserves_password_whitespace(monkeypatch):
    captured: dict[str, str] = {}

    def fake_login(username, password, year, base_url):
        captured["username"] = username
        captured["password"] = password
        return "tok_from_creds", {"userid": 5, "fullname": "Sahar", "username": username}

    monkeypatch.setattr("runi_moodle_cli.auth.login_with_credentials", fake_login)

    # Default (no flag) with 2-line stdin
    res1 = runner.invoke(app, ["auth", "login", "--year", "2027", "--pretty"], input="sahar.zolberg\n  pass with spaces \n")
    assert res1.exit_code == 0, res1.stdout
    data1 = json.loads(res1.stdout)
    assert data1["method"] == "credentials"
    assert "\n  " in res1.stdout  # --pretty indented JSON
    assert captured == {"username": "sahar.zolberg", "password": "  pass with spaces "}

    # --username with password on stdin also preserves spaces
    res2 = runner.invoke(app, ["auth", "login", "--year", "2027", "-u", "sahar.zolberg", "--json"], input="  lead_and_trail  \n")
    assert res2.exit_code == 0, res2.stdout
    assert json.loads(res2.stdout)["ok"] is True
    assert captured["password"] == "  lead_and_trail  "

    # Single-line piped stdin with no flag gives a helpful hint naming --cookie / --token
    res3 = runner.invoke(app, ["auth", "login", "--year", "2027", "--json"], input="only_one_line\n")
    assert res3.exit_code == 3
    err_msg = json.loads(res3.stdout)["error"]["message"]
    assert "--cookie" in err_msg and "--token" in err_msg


def test_auth_login_tty_prompts_go_to_stderr_keeping_json_stdout_clean(monkeypatch):
    monkeypatch.setattr(
        "runi_moodle_cli.auth.login_with_credentials",
        lambda u, p, year, base_url: ("tok_tty", {"userid": 8, "fullname": "Sahar", "username": u}),
    )
    prompt_calls: list[dict] = []

    def fake_prompt(text, hide_input=False, err=False):
        prompt_calls.append({"text": text, "hide_input": hide_input, "err": err})
        return "sahar.zolberg" if not hide_input else "pw"

    monkeypatch.setattr("runi_moodle_cli.cli._stdin_is_tty", lambda: True)
    monkeypatch.setattr("typer.prompt", fake_prompt)

    res = runner.invoke(app, ["auth", "login", "--year", "2027", "--pretty"])
    assert res.exit_code == 0, res.stdout
    payload = json.loads(res.stdout)
    assert payload["ok"] is True
    assert payload["method"] == "credentials"
    assert all(call["err"] is True for call in prompt_calls)


def test_auth_login_flag_validation_and_mobile_token_url(monkeypatch):
    res_conflict = runner.invoke(app, ["auth", "login", "--credentials", "--cookie", "--json"])
    assert res_conflict.exit_code == 2

    res_user_cookie = runner.invoke(app, ["auth", "login", "--cookie", "-u", "sahar", "--json"])
    assert res_user_cookie.exit_code == 2

    res_empty_tok = runner.invoke(app, ["auth", "login", "--token", "--json"], input="   \n")
    assert res_empty_tok.exit_code == 3

    b64_tok = base64.b64encode(b"sig:::direct_mobile_tok").decode()
    monkeypatch.setattr(
        "runi_moodle_cli.auth.verify_token",
        lambda year, token, base_url: {"userid": 11, "fullname": "Sahar", "username": "sahar"},
    )
    res_tok = runner.invoke(
        app,
        ["auth", "login", "--year", "2027", "--token", "--json"],
        input=f"MOODLEMOBILE://token={b64_tok}\n",
    )
    assert res_tok.exit_code == 0, res_tok.stdout
    assert json.loads(res_tok.stdout)["method"] == "token"


def test_subcommand_year_overrides_root_year(monkeypatch):
    seen: list[int | None] = []

    def fake_list_courses(semester=None, year=None):
        seen.append(year)
        return []

    monkeypatch.setattr("runi_moodle_cli.courses.list_courses", fake_list_courses)
    res = runner.invoke(app, ["--year", "2026", "courses", "--year", "2027", "--json"])
    assert res.exit_code == 0, res.stdout
    assert seen == [2027]


def test_lang_flag_defaults_en_and_supports_he_auto_and_validation(configured_site, moodle_api):
    seen_bodies: list[str] = []

    def handler(request: httpx.Request):
        body = request.content.decode()
        seen_bodies.append(body)
        return httpx.Response(200, json={"courses": []})

    moodle_api.post("https://moodle.runi.ac.il/2027/webservice/rest/server.php").mock(side_effect=handler)

    r_default = runner.invoke(app, ["courses", "--json"])
    assert r_default.exit_code == 0
    assert "moodlewssettinglang=en" in seen_bodies[-1]

    r_he = runner.invoke(app, ["courses", "--lang", "he", "--json"])
    assert r_he.exit_code == 0
    assert "moodlewssettinglang=he" in seen_bodies[-1]

    r_auto = runner.invoke(app, ["courses", "--lang", "auto", "--json"])
    assert r_auto.exit_code == 0
    assert "moodlewssettinglang=" not in seen_bodies[-1]

    r_bad = runner.invoke(app, ["courses", "--lang", "fr", "--json"])
    assert r_bad.exit_code == 2
