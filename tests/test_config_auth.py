"""Tests for cross-platform config and Moodle mobile token authentication."""

from __future__ import annotations

import base64

import httpx
import pytest

from runi_moodle_cli.auth import (
    _form_data,
    _parse_forms,
    exchange_cookie_for_token,
    extract_cookie_value,
    login_with_credentials,
    parse_mobile_redirect,
)
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


def test_login_with_credentials_saml_happy_path(moodle_api):
    payload = base64.b64encode(b"sig:::creds_long_token").decode()
    launch_calls = {"n": 0}

    def launch_handler(request: httpx.Request):
        launch_calls["n"] += 1
        if launch_calls["n"] == 1:
            return httpx.Response(303, headers={"location": "https://auth.runi.ac.il/saml/sso?SAMLRequest=abc"})
        return httpx.Response(303, headers={"location": f"moodlemobile://token={payload}"})

    moodle_api.get("https://moodle.runi.ac.il/2027/admin/tool/mobile/launch.php").mock(side_effect=launch_handler)
    moodle_api.get("https://auth.runi.ac.il/saml/sso").mock(
        return_value=httpx.Response(302, headers={"location": "https://my.runi.ac.il/my.policy"})
    )
    moodle_api.get("https://my.runi.ac.il/my.policy").mock(
        return_value=httpx.Response(
            200,
            text=(
                '<form id="auth_form" method="post">'
                '<input type="text" name="username">'
                '<input type="password" name="password">'
                '<input type="hidden" name="vhost" value="standard">'
                "</form>"
            ),
        )
    )

    def policy_post(request: httpx.Request):
        body = request.content.decode()
        assert "username=sahar" in body
        assert "password=secret" in body
        assert "vhost=standard" in body
        return httpx.Response(302, headers={"location": "https://auth.runi.ac.il/saml/resp"})

    moodle_api.post("https://my.runi.ac.il/my.policy").mock(side_effect=policy_post)
    moodle_api.get("https://auth.runi.ac.il/saml/resp").mock(
        return_value=httpx.Response(
            200,
            text=(
                '<form method="post" action="https://moodle.runi.ac.il/2027/auth/saml2/sp/saml2-acs.php/moodle.runi.ac.il">'
                '<input type="hidden" name="SAMLResponse" value="PHNhbWw+">'
                '<input type="hidden" name="RelayState" value="https://moodle.runi.ac.il/2027/admin/tool/mobile/launch.php?service=moodle_mobile_app&amp;passport=1">'
                "</form>"
            ),
        )
    )
    moodle_api.post("https://moodle.runi.ac.il/2027/auth/saml2/sp/saml2-acs.php/moodle.runi.ac.il").mock(
        return_value=httpx.Response(
            303,
            headers={"location": "https://moodle.runi.ac.il/2027/admin/tool/mobile/launch.php?service=moodle_mobile_app&passport=1"},
        )
    )
    moodle_api.post("https://moodle.runi.ac.il/2027/webservice/rest/server.php").mock(
        return_value=httpx.Response(200, json={"userid": 99, "fullname": "Sahar", "username": "sahar"})
    )

    token, info = login_with_credentials("sahar", "secret", year=2027)
    assert token == "creds_long_token"
    assert info["userid"] == 99


def test_login_with_credentials_invalid_password(moodle_api):
    login_html = (
        '<form method="post">'
        '<input type="text" name="username">'
        '<input type="password" name="password">'
        '<input type="hidden" name="vhost" value="standard">'
        "</form>"
    )
    moodle_api.get("https://moodle.runi.ac.il/2027/admin/tool/mobile/launch.php").mock(
        return_value=httpx.Response(303, headers={"location": "https://my.runi.ac.il/my.policy"})
    )
    moodle_api.get("https://my.runi.ac.il/my.policy").mock(return_value=httpx.Response(200, text=login_html))
    moodle_api.post("https://my.runi.ac.il/my.policy").mock(return_value=httpx.Response(200, text=login_html))

    with pytest.raises(AuthError, match="Invalid RUNI username or password"):
        login_with_credentials("sahar", "wrong", year=2027)


def test_login_with_credentials_unexpected_mfa_step(moodle_api):
    login_html = '<form method="post"><input name="username"><input name="password"></form>'
    mfa_html = '<form method="post"><input name="otp_code"></form>'
    moodle_api.get("https://moodle.runi.ac.il/2027/admin/tool/mobile/launch.php").mock(
        return_value=httpx.Response(303, headers={"location": "https://my.runi.ac.il/my.policy?secret=should_not_leak"})
    )
    moodle_api.get("https://my.runi.ac.il/my.policy").mock(return_value=httpx.Response(200, text=login_html))
    moodle_api.post("https://my.runi.ac.il/my.policy").mock(return_value=httpx.Response(200, text=mfa_html))

    with pytest.raises(AuthError, match="MFA") as exc_info:
        login_with_credentials("sahar", "secret", year=2027)
    assert "should_not_leak" not in str(exc_info.value)


def test_login_with_credentials_f5_logout_errorcode(moodle_api):
    login_html = '<form method="post"><input name="username"><input name="password"></form>'
    moodle_api.get("https://moodle.runi.ac.il/2027/admin/tool/mobile/launch.php").mock(
        return_value=httpx.Response(303, headers={"location": "https://my.runi.ac.il/my.policy"})
    )
    moodle_api.get("https://my.runi.ac.il/my.policy").mock(return_value=httpx.Response(200, text=login_html))
    moodle_api.post("https://my.runi.ac.il/my.policy").mock(
        return_value=httpx.Response(302, headers={"location": "https://my.runi.ac.il/my.logout.php3?errorcode=19"})
    )
    moodle_api.get("https://my.runi.ac.il/my.logout.php3").mock(
        return_value=httpx.Response(200, text="<html>Session expired</html>")
    )

    with pytest.raises(AuthError, match="Invalid RUNI username or password"):
        login_with_credentials("sahar", "wrong", year=2027)


def test_login_with_credentials_rejects_untrusted_redirect_and_http_downgrade(moodle_api):
    evil_route = moodle_api.get("https://evil.example/phish").mock(return_value=httpx.Response(200))
    moodle_api.get("https://moodle.runi.ac.il/2027/admin/tool/mobile/launch.php").mock(
        return_value=httpx.Response(303, headers={"location": "https://evil.example/phish?x=1"})
    )
    with pytest.raises(AuthError, match="untrusted URL") as exc_info:
        login_with_credentials("sahar", "secret", year=2027)
    assert evil_route.call_count == 0
    assert "x=1" not in str(exc_info.value)

    http_route = moodle_api.get("http://my.runi.ac.il/my.policy").mock(return_value=httpx.Response(200))
    moodle_api.get("https://moodle.runi.ac.il/2027/admin/tool/mobile/launch.php").mock(
        return_value=httpx.Response(303, headers={"location": "http://my.runi.ac.il/my.policy"})
    )
    with pytest.raises(AuthError, match="untrusted URL"):
        login_with_credentials("sahar", "secret", year=2027)
    assert http_route.call_count == 0


def test_login_with_credentials_rejects_untrusted_form_actions_and_get_method(moodle_api):
    evil_post = moodle_api.post("https://evil.example/steal").mock(return_value=httpx.Response(200))
    moodle_api.get("https://moodle.runi.ac.il/2027/admin/tool/mobile/launch.php").mock(
        return_value=httpx.Response(303, headers={"location": "https://my.runi.ac.il/my.policy"})
    )
    moodle_api.get("https://my.runi.ac.il/my.policy").mock(
        return_value=httpx.Response(
            200,
            text='<form method="post" action="https://evil.example/steal"><input name="username"><input name="password"></form>',
        )
    )
    with pytest.raises(AuthError, match="untrusted URL"):
        login_with_credentials("sahar", "secret", year=2027)
    assert evil_post.call_count == 0

    # Non-POST credential form is also rejected
    moodle_api.get("https://my.runi.ac.il/my.policy").mock(
        return_value=httpx.Response(
            200,
            text='<form method="get" action="https://my.runi.ac.il/my.policy"><input name="username"><input name="password"></form>',
        )
    )
    with pytest.raises(AuthError, match="non-POST"):
        login_with_credentials("sahar", "secret", year=2027)


def test_login_with_credentials_rejects_untrusted_saml_action_and_foreign_mobile_callback(moodle_api):
    evil_acs = moodle_api.post("https://evil.example/acs").mock(return_value=httpx.Response(200))
    moodle_api.get("https://moodle.runi.ac.il/2027/admin/tool/mobile/launch.php").mock(
        return_value=httpx.Response(303, headers={"location": "https://my.runi.ac.il/my.policy"})
    )
    moodle_api.get("https://my.runi.ac.il/my.policy").mock(
        return_value=httpx.Response(200, text='<form method="post"><input name="username"><input name="password"></form>')
    )
    moodle_api.post("https://my.runi.ac.il/my.policy").mock(
        return_value=httpx.Response(
            200,
            text='<form method="post" action="https://evil.example/acs"><input type="hidden" name="SAMLResponse" value="abc"></form>',
        )
    )
    with pytest.raises(AuthError, match="untrusted URL"):
        login_with_credentials("sahar", "secret", year=2027)
    assert evil_acs.call_count == 0

    # moodlemobile:// returned by a non-Moodle host is rejected
    b64_tok = base64.b64encode(b"sig:::injected").decode()
    moodle_api.post("https://my.runi.ac.il/my.policy").mock(
        return_value=httpx.Response(302, headers={"location": f"moodlemobile://token={b64_tok}"})
    )
    with pytest.raises(AuthError, match="untrusted origin"):
        login_with_credentials("sahar", "secret", year=2027)


def test_login_with_credentials_prelogin_saml_request_and_html_fallback(moodle_api):
    payload = base64.b64encode(b"sig:::html_fallback_token").decode()
    launch_calls = {"n": 0}

    def launch_handler(request: httpx.Request):
        launch_calls["n"] += 1
        if launch_calls["n"] == 1:
            return httpx.Response(
                200,
                text=(
                    '<form method="post" action="https://auth.runi.ac.il/saml/post">'
                    '<input type="hidden" name="SAMLRequest" value="req123">'
                    "</form>"
                ),
            )
        return httpx.Response(200, text=f'<a href="moodlemobile://token={payload}">Open app</a>')

    moodle_api.get("https://moodle.runi.ac.il/2027/admin/tool/mobile/launch.php").mock(side_effect=launch_handler)
    moodle_api.post("https://auth.runi.ac.il/saml/post").mock(
        return_value=httpx.Response(302, headers={"location": "https://my.runi.ac.il/my.policy"})
    )
    moodle_api.get("https://my.runi.ac.il/my.policy").mock(
        return_value=httpx.Response(200, text='<form method="post"><input name="username"><input name="password"></form>')
    )
    moodle_api.post("https://my.runi.ac.il/my.policy").mock(
        return_value=httpx.Response(
            302,
            headers={"location": "https://moodle.runi.ac.il/2027/admin/tool/mobile/launch.php?service=moodle_mobile_app"},
        )
    )
    moodle_api.post("https://moodle.runi.ac.il/2027/webservice/rest/server.php").mock(
        return_value=httpx.Response(200, json={"userid": 12, "fullname": "Test", "username": "sahar"})
    )

    token, info = login_with_credentials("sahar", "secret", year=2027)
    assert token == "html_fallback_token"
    assert info["userid"] == 12


def test_login_with_credentials_redirect_loop_and_307_rejected(moodle_api):
    moodle_api.get("https://moodle.runi.ac.il/2027/admin/tool/mobile/launch.php").mock(
        return_value=httpx.Response(302, headers={"location": "https://my.runi.ac.il/loop"})
    )
    moodle_api.get("https://my.runi.ac.il/loop").mock(
        return_value=httpx.Response(302, headers={"location": "https://my.runi.ac.il/loop"})
    )
    with pytest.raises(AuthError, match="Too many redirects"):
        login_with_credentials("sahar", "secret", year=2027)

    moodle_api.get("https://moodle.runi.ac.il/2027/admin/tool/mobile/launch.php").mock(
        return_value=httpx.Response(307, headers={"location": "https://my.runi.ac.il/my.policy"})
    )
    with pytest.raises(AuthError, match="307"):
        login_with_credentials("sahar", "secret", year=2027)


def test_parse_forms_filters_disabled_unchecked_and_submit():
    html = (
        '<form method="post">'
        '<input type="hidden" name="vhost" value="standard">'
        '<input type="text" name="disabled_field" value="x" disabled>'
        '<input type="checkbox" name="remember" value="1">'
        '<input type="checkbox" name="agree" value="yes" checked>'
        '<input type="submit" name="go" value="Login">'
        "</form>"
    )
    forms = _parse_forms(html)
    assert len(forms) == 1
    assert _form_data(forms[0]) == {"vhost": "standard", "agree": "yes"}


def test_parse_mobile_redirect_url_encoded_and_fragment():
    payload = base64.b64encode(b"sig123:::tok_with_eq").decode()
    encoded = payload.replace("=", "%3D")
    assert parse_mobile_redirect(f"MOODLEMOBILE://token={encoded}#done") == "tok_with_eq"


def test_login_with_credentials_pins_runi_origin_port_and_redacts_userinfo(moodle_api):
    # Custom --base-url is rejected before making any network request
    evil_launch = moodle_api.get("https://evil.example/2027/admin/tool/mobile/launch.php").mock(
        return_value=httpx.Response(200)
    )
    with pytest.raises(AuthError, match="only supported for https://moodle.runi.ac.il"):
        login_with_credentials("sahar", "secret", year=2027, base_url="https://evil.example")
    assert evil_launch.call_count == 0

    # Explicit :0 port is also rejected (must not be treated as falsy default 443)
    with pytest.raises(AuthError, match="only supported for https://moodle.runi.ac.il"):
        login_with_credentials("sahar", "secret", year=2027, base_url="https://moodle.runi.ac.il:0")

    # Non-443 port on an otherwise allowed host is rejected
    alt_port = moodle_api.get("https://my.runi.ac.il:8443/my.policy").mock(return_value=httpx.Response(200))
    moodle_api.get("https://moodle.runi.ac.il/2027/admin/tool/mobile/launch.php").mock(
        return_value=httpx.Response(303, headers={"location": "https://my.runi.ac.il:8443/my.policy"})
    )
    with pytest.raises(AuthError, match="untrusted URL"):
        login_with_credentials("sahar", "secret", year=2027)
    assert alt_port.call_count == 0

    # URL userinfo in a rejected redirect is stripped from the error message
    moodle_api.get("https://moodle.runi.ac.il/2027/admin/tool/mobile/launch.php").mock(
        return_value=httpx.Response(
            303,
            headers={"location": "https://alice:topsecret@evil.example/phish?token=leak"},
        )
    )
    with pytest.raises(AuthError, match="https://evil.example/phish") as exc_info:
        login_with_credentials("sahar", "secret", year=2027)
    msg = str(exc_info.value)
    assert "alice" not in msg and "topsecret" not in msg and "leak" not in msg

    # Credential POST is pinned strictly to my.runi.ac.il:443 (not moodle.runi.ac.il)
    moodle_post = moodle_api.post("https://moodle.runi.ac.il/2027/login/index.php").mock(
        return_value=httpx.Response(200)
    )
    moodle_api.get("https://moodle.runi.ac.il/2027/admin/tool/mobile/launch.php").mock(
        return_value=httpx.Response(303, headers={"location": "https://my.runi.ac.il/my.policy"})
    )
    moodle_api.get("https://my.runi.ac.il/my.policy").mock(
        return_value=httpx.Response(
            200,
            text='<form method="post" action="https://moodle.runi.ac.il/2027/login/index.php"><input name="username"><input name="password"></form>',
        )
    )
    with pytest.raises(AuthError, match="untrusted URL"):
        login_with_credentials("sahar", "secret", year=2027)
    assert moodle_post.call_count == 0
