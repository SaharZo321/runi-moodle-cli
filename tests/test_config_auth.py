"""Tests for cross-platform config and Moodle mobile token authentication."""

from __future__ import annotations

import base64

import httpx
import pytest

from runi_moodle_cli.auth import exchange_cookie_for_token, extract_cookie_value, parse_mobile_redirect
from runi_moodle_cli.config import AuthError, get_site, load_config, remove_token, save_token


def test_missing_config_raises_auth_error():
    with pytest.raises(AuthError, match="runi auth login"):
        get_site()


def test_save_load_and_remove_tokens():
    save_token(2027, "abc123token")
    save_token(2026, "oldtoken", set_default=False)

    cfg = load_config()
    assert cfg.default_year == 2027
    assert cfg.tokens == {2026: "oldtoken", 2027: "abc123token"}
    assert get_site().url == "https://moodle.runi.ac.il/2027"
    assert get_site(2026).token == "oldtoken"

    remove_token(2027)
    cfg = load_config()
    assert cfg.default_year == 2026
    assert 2027 not in cfg.tokens


def test_env_overrides(monkeypatch):
    save_token(2027, "filetoken")
    monkeypatch.setenv("MOODLE_TOKEN_2027", "envtoken")
    monkeypatch.setenv("MOODLE_DEFAULT_YEAR", "2027")
    assert get_site().token == "envtoken"


def test_extract_cookie_value_formats():
    assert extract_cookie_value("xyz987") == "xyz987"
    assert extract_cookie_value("MoodleSession2027=xyz987") == "xyz987"
    assert extract_cookie_value("Cookie: GCLB=1; MoodleSession2027=xyz987; foo=bar") == "xyz987"


def test_parse_mobile_redirect():
    payload = base64.b64encode(b"sig123:::my_real_wstoken:::priv").decode()
    assert parse_mobile_redirect(f"moodlemobile://token={payload}") == "my_real_wstoken"


def test_exchange_cookie_for_token_success(moodle_api):
    payload = base64.b64encode(b"sig123:::long_lived_token_123").decode()
    moodle_api.get("https://moodle.runi.ac.il/2027/admin/tool/mobile/launch.php").mock(
        return_value=httpx.Response(303, headers={"location": f"moodlemobile://token={payload}"})
    )
    moodle_api.post("https://moodle.runi.ac.il/2027/webservice/rest/server.php").mock(
        return_value=httpx.Response(200, json={"userid": 42, "fullname": "Test Student", "username": "test"})
    )

    token, info = exchange_cookie_for_token("MoodleSession2027=sess_cookie", year=2027)
    assert token == "long_lived_token_123"
    assert info["userid"] == 42


def test_exchange_cookie_rejected_redirects_to_login(moodle_api):
    moodle_api.get("https://moodle.runi.ac.il/2027/admin/tool/mobile/launch.php").mock(
        return_value=httpx.Response(303, headers={"location": "https://moodle.runi.ac.il/2027/login/index.php"})
    )
    with pytest.raises(AuthError, match="rejected or expired"):
        exchange_cookie_for_token("expired_cookie", year=2027)
