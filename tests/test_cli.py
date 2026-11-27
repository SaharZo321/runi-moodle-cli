"""End-to-end CLI tests via Typer's CliRunner."""

from __future__ import annotations

import base64
import json

import httpx
from typer.testing import CliRunner

from moodle_cli.cli import app

runner = CliRunner()


def test_version():
    res = runner.invoke(app, ["--version"])
    assert res.exit_code == 0
    assert "moodle-cli" in res.stdout


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
        "moodle_cli.reading.read_content",
        lambda url, offset=0, year=None: {"name": "notes.pdf", "text": "Hello [world]\nLine 2", "total_chars": 20},
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
        "moodle_cli.reading.read_content",
        lambda url, offset=0, year=None: {
            "name": "Week [draft].pdf",
            "text": "Status: :warning: [not markup]",
            "total_chars": 30,
        },
    )
    monkeypatch.setattr("moodle_cli.cli._stdout_is_tty", lambda: True)

    res = runner.invoke(app, ["read", "https://moodle.runi.ac.il/2027/pluginfile.php/1/notes.pdf"])
    assert res.exit_code == 0, res.stdout
    assert "Week [draft].pdf" in res.stdout
    assert ":warning:" in res.stdout
    assert "[not markup]" in res.stdout


def test_read_piped_error_is_human_not_json(monkeypatch):
    from moodle_cli.client import MoodleError

    def boom(url, offset=0, year=None):
        raise MoodleError("File not found")

    monkeypatch.setattr("moodle_cli.reading.read_content", boom)
    res = runner.invoke(app, ["read", "https://moodle.runi.ac.il/2027/pluginfile.php/1/missing.pdf"])
    assert res.exit_code == 4
    assert "File not found" in res.output
    assert '"ok": false' not in res.stdout


def test_auth_login_default_credentials_and_preserves_password_whitespace(monkeypatch):
    captured: dict[str, str] = {}

    def fake_login(username, password, year, base_url, site_url=None):
        captured["username"] = username
        captured["password"] = password
        return "tok_from_creds", {"userid": 5, "fullname": "Sahar", "username": username}

    monkeypatch.setattr("moodle_cli.auth.login_with_credentials", fake_login)

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
        "moodle_cli.auth.login_with_credentials",
        lambda u, p, year, base_url, site_url=None: ("tok_tty", {"userid": 8, "fullname": "Sahar", "username": u}),
    )
    prompt_calls: list[dict] = []

    def fake_prompt(text, hide_input=False, err=False):
        prompt_calls.append({"text": text, "hide_input": hide_input, "err": err})
        return "sahar.zolberg" if not hide_input else "pw"

    monkeypatch.setattr("moodle_cli.cli._stdin_is_tty", lambda: True)
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
        "moodle_cli.auth.verify_token",
        lambda year, token, base_url, site_url=None: {"userid": 11, "fullname": "Sahar", "username": "sahar"},
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

    monkeypatch.setattr("moodle_cli.courses.list_courses", fake_list_courses)
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

    r_fr = runner.invoke(app, ["courses", "--lang", "fr", "--json"])
    assert r_fr.exit_code == 0
    assert "moodlewssettinglang=fr" in seen_bodies[-1]

    r_auto = runner.invoke(app, ["courses", "--lang", "auto", "--json"])
    assert r_auto.exit_code == 0
    assert "moodlewssettinglang=" not in seen_bodies[-1]

    r_bad = runner.invoke(app, ["courses", "--lang", "en&evil=1", "--json"])
    assert r_bad.exit_code == 2


def test_cli_escapes_rich_markup_and_renders_type_tags(monkeypatch):
    monkeypatch.setattr("moodle_cli.cli._stdout_is_tty", lambda: True)
    monkeypatch.setattr(
        "moodle_cli.courses.resolve_course",
        lambda q, y=None: {"id": 101, "name": "Course [/red][bold]"},
    )
    monkeypatch.setattr(
        "moodle_cli.courses.get_course_contents",
        lambda cid, y=None: [
            {
                "section": "Week [/x]",
                "items": [{"id": 1, "type": "resource", "name": "Slides [/cyan]", "files": []}],
            }
        ],
    )

    res = runner.invoke(app, ["course", "101", "contents"])
    assert res.exit_code == 0, res.output
    assert "[resource]" in res.stdout
    assert "Slides [/cyan]" in res.stdout
    assert "Course [/red][bold]" in res.stdout


def test_cli_download_batch_exits_1_on_partial_errors(monkeypatch, tmp_path):
    monkeypatch.setattr("moodle_cli.courses.resolve_course", lambda q, y=None: {"id": 101, "name": "Net"})
    monkeypatch.setattr(
        "moodle_cli.reading.download_course_files",
        lambda cid, dest_dir, year, section_filter, overwrite: {
            "downloaded": [],
            "skipped": [],
            "errors": [{"filename": "bad.pdf", "url": "https://moodle.runi.ac.il/2027/f", "error": "HTTP 404"}],
        },
    )
    res = runner.invoke(app, ["download", "101", "-o", str(tmp_path), "--json"])
    assert res.exit_code == 1
    assert json.loads(res.stdout)["errors"][0]["filename"] == "bad.pdf"


def test_auth_login_with_env_base_url_saves_token_under_same_origin(monkeypatch):
    from moodle_cli.config import load_config

    monkeypatch.setenv("MOODLE_BASE_URL", "https://custom.moodle.example")
    monkeypatch.setattr(
        "moodle_cli.auth.verify_token",
        lambda year, token, base_url, site_url=None: {"userid": 3, "fullname": "User", "username": "u"},
    )
    res = runner.invoke(app, ["auth", "login", "--year", "2027", "--token", "--json"], input="tok_custom\n")
    assert res.exit_code == 0, res.stdout

    # Removing MOODLE_BASE_URL must still show the saved token bound to custom.moodle.example, not RUNI
    monkeypatch.delenv("MOODLE_BASE_URL", raising=False)
    cfg = load_config()
    assert cfg.base_url == "https://custom.moodle.example"
    assert cfg.tokens == {2027: "tok_custom"}


def test_read_tty_strips_terminal_escape_sequences(monkeypatch):
    monkeypatch.setattr("moodle_cli.cli._stdout_is_tty", lambda: True)
    monkeypatch.setattr(
        "moodle_cli.reading.read_content",
        lambda url, offset=0, year=None: {"name": "a\x1b]0;bad\x07.pdf", "text": "ok\x1b[31mred\x07", "total_chars": 12},
    )
    res = runner.invoke(app, ["read", "https://moodle.runi.ac.il/2027/pluginfile.php/1/a.pdf"])
    assert res.exit_code == 0, res.output
    assert "\x1b" not in res.stdout
    assert "\x07" not in res.stdout

    # Messages text also strips ESC / C1 control sequences
    monkeypatch.setattr(
        "moodle_cli.messages.staff_messages",
        lambda days, cid, year: [
            {"time": "2026-12-01 10:00", "kind": "direct_message", "author": "Alice", "text": "hi\x1b[2J\x9bthere"}
        ],
    )
    res_msg = runner.invoke(app, ["messages"])
    assert res_msg.exit_code == 0, res_msg.output
    assert "\x1b" not in res_msg.stdout and "\x9b" not in res_msg.stdout
    assert "[direct_message]" in res_msg.stdout


def test_auth_login_generic_university_site_and_status(monkeypatch, moodle_api):
    from moodle_cli.config import get_site

    moodle_api.post("https://moodle.other.edu/login/token.php").mock(
        return_value=httpx.Response(200, json={"token": "other_uni_tok"})
    )
    moodle_api.post("https://moodle.other.edu/webservice/rest/server.php").mock(
        return_value=httpx.Response(200, json={"userid": 42, "fullname": "Ada", "username": "ada"})
    )

    # --site and --base-url together are rejected
    conflict = runner.invoke(
        app,
        ["auth", "login", "--site", "https://moodle.other.edu", "--base-url", "https://moodle.runi.ac.il", "--json"],
    )
    assert conflict.exit_code == 2

    res = runner.invoke(
        app,
        ["auth", "login", "--site", "https://moodle.other.edu", "--year", "2027", "--json"],
        input="ada\nsecretpw\n",
    )
    assert res.exit_code == 0, res.stdout
    data = json.loads(res.stdout)
    assert data["site"] == "https://moodle.other.edu"
    assert get_site(2027).url == "https://moodle.other.edu"

    status_res = runner.invoke(app, ["auth", "status", "--no-check", "--json"])
    assert status_res.exit_code == 0, status_res.stdout
    assert json.loads(status_res.stdout)["years"][0]["url"] == "https://moodle.other.edu"

    # Signing in to a second year reuses the existing exact site URL instead of appending /2028
    res_y2 = runner.invoke(
        app,
        ["auth", "login", "--year", "2028", "--json"],
        input="ada\nsecretpw\n",
    )
    assert res_y2.exit_code == 0, res_y2.stdout
    assert json.loads(res_y2.stdout)["site"] == "https://moodle.other.edu"
    assert get_site(2028).url == "https://moodle.other.edu"

    # Origin conflict is rejected BEFORE prompting for password
    prompted = {"called": False}

    def fail_if_prompted(*args, **kwargs):
        prompted["called"] = True
        return "x"

    monkeypatch.setattr("moodle_cli.cli._stdin_is_tty", lambda: True)
    monkeypatch.setattr("typer.prompt", fail_if_prompted)
    res_conflict_origin = runner.invoke(
        app,
        ["auth", "login", "--site", "https://moodle.third.edu", "--json"],
    )
    assert res_conflict_origin.exit_code == 3
    assert "already holds tokens" in json.loads(res_conflict_origin.stdout)["error"]["message"]
    assert prompted["called"] is False


def test_reauth_year_based_token_does_not_borrow_other_years_site_url(moodle_api):
    from moodle_cli.config import get_site, save_token

    save_token(2026, "tok_2026", base_url="https://moodle.uni.edu")
    save_token(2027, "tok_2027", site_url="https://moodle.uni.edu/special")

    moodle_api.post("https://moodle.uni.edu/2026/webservice/rest/server.php").mock(
        return_value=httpx.Response(200, json={"userid": 1, "fullname": "U", "username": "u"})
    )
    res = runner.invoke(
        app,
        ["auth", "login", "--year", "2026", "--token", "--json"],
        input="new_tok_2026\n",
    )
    assert res.exit_code == 0, res.stdout
    assert json.loads(res.stdout)["site"] == "https://moodle.uni.edu/2026"
    assert get_site(2026).url == "https://moodle.uni.edu/2026"
    assert get_site(2027).url == "https://moodle.uni.edu/special"


def test_new_year_after_base_url_path_change_uses_new_base_not_preserved_old_year(monkeypatch, moodle_api):
    from moodle_cli.config import get_site, load_config, remove_token, save_token

    # Even with --no-default on 2026 AND reauthenticating preserved 2025 without URL flags,
    # a new 2027 login must use the updated base_url (/b/2027), never /a/2025.
    save_token(2025, "tok_2025", base_url="https://m.example.edu/a")
    save_token(2026, "tok_2026", base_url="https://m.example.edu/b", set_default=False)
    moodle_api.post("https://m.example.edu/a/2025/webservice/rest/server.php").mock(
        return_value=httpx.Response(200, json={"userid": 1, "fullname": "U", "username": "u"})
    )
    reauth_2025 = runner.invoke(
        app,
        ["auth", "login", "--year", "2025", "--token", "--json"],
        input="tok_2025_v2\n",
    )
    assert reauth_2025.exit_code == 0, reauth_2025.stdout
    assert load_config().default_site_url is None

    moodle_api.post("https://m.example.edu/b/2027/webservice/rest/server.php").mock(
        return_value=httpx.Response(200, json={"userid": 2, "fullname": "U", "username": "u"})
    )
    res = runner.invoke(
        app,
        ["auth", "login", "--year", "2027", "--token", "--json"],
        input="tok_2027\n",
    )
    assert res.exit_code == 0, res.stdout
    assert json.loads(res.stdout)["site"] == "https://m.example.edu/b/2027"
    assert get_site(2027).url == "https://m.example.edu/b/2027"
    assert get_site(2025).url == "https://m.example.edu/a/2025"

    # MOODLE_BASE_URL clears saved default_site_url so it is never silently ignored
    save_token(2028, "tok_site", site_url="https://m.example.edu/lms")
    assert load_config().default_site_url == "https://m.example.edu/lms"
    monkeypatch.setenv("MOODLE_BASE_URL", "https://m.example.edu/other")
    assert load_config().default_site_url is None
    monkeypatch.delenv("MOODLE_BASE_URL", raising=False)

    # Removing the default --site year clears default_site_url when year-based tokens remain
    remove_token(2028)
    assert load_config().default_site_url is None

    # Removing the default year keeps default_site_url when another remaining year still uses that exact site
    save_token(2028, "tok_s1", site_url="https://m.example.edu/lms", set_default_site=True)
    save_token(2029, "tok_s2", site_url="https://m.example.edu/lms", set_default_site=False)
    remove_token(2029)
    assert load_config().default_site_url == "https://m.example.edu/lms"

    # Replacing the only remaining exact-site binding via --base-url --no-default clears default_site_url
    save_token(2028, "tok_b", base_url="https://m.example.edu/archive", set_default=False)
    assert load_config().default_site_url is None


def test_auth_login_fresh_with_moodle_site_url_env(monkeypatch, moodle_api):
    from moodle_cli.config import get_site

    monkeypatch.setenv("MOODLE_SITE_URL", "https://moodle.fresh.edu/lms")
    moodle_api.post("https://moodle.fresh.edu/lms/login/token.php").mock(
        return_value=httpx.Response(200, json={"token": "fresh_env_tok"})
    )
    moodle_api.post("https://moodle.fresh.edu/lms/webservice/rest/server.php").mock(
        return_value=httpx.Response(200, json={"userid": 5, "fullname": "Fresh", "username": "fresh"})
    )

    res = runner.invoke(app, ["auth", "login", "--year", "2027", "--json"], input="fresh\npw\n")
    assert res.exit_code == 0, res.stdout
    assert json.loads(res.stdout)["site"] == "https://moodle.fresh.edu/lms"
    monkeypatch.delenv("MOODLE_SITE_URL", raising=False)
    assert get_site(2027).url == "https://moodle.fresh.edu/lms"


def test_pyproject_script_aliases_present():
    import tomllib
    from pathlib import Path

    data = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = data["project"]["scripts"]
    for name in ("mdl", "moodle-cli", "runi", "runi-moodle-cli"):
        assert scripts[name] == "moodle_cli.cli:main"
