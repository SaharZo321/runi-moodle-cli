"""Authentication helpers for obtaining and verifying long-lived Moodle Mobile tokens."""

from __future__ import annotations

import base64
from html.parser import HTMLParser
import re
import secrets
from typing import Any
from urllib.parse import unquote, urlencode, urljoin, urlparse

import httpx

from runi_moodle_cli.client import get_site_info
from runi_moodle_cli.config import AuthError, DEFAULT_BASE_URL, Site

COOKIE_PAIR_RE = re.compile(r"MoodleSession\d*=([^;\s'\"]+)", re.I)
MOBILE_TOKEN_LINK_RE = re.compile(r"moodlemobile://token=([A-Za-z0-9+/=%._-]+)", re.I)
RUNI_MOODLE_ORIGIN = ("https", "moodle.runi.ac.il", 443)
RUNI_CREDENTIAL_ORIGINS = frozenset({("https", "my.runi.ac.il", 443)})
RUNI_SSO_ORIGINS = frozenset(
    {
        RUNI_MOODLE_ORIGIN,
        ("https", "auth.runi.ac.il", 443),
        ("https", "my.runi.ac.il", 443),
    }
)


def launch_url(year: int, base_url: str = DEFAULT_BASE_URL, passport: str | None = None) -> str:
    passport = passport or secrets.token_hex(8)
    query = urlencode(
        {
            "service": "moodle_mobile_app",
            "passport": passport,
            "urlscheme": "moodlemobile",
        }
    )
    return f"{base_url.rstrip('/')}/{year}/admin/tool/mobile/launch.php?{query}"


def parse_mobile_redirect(location: str) -> str:
    """Extract the wstoken from a moodlemobile://token=<base64> redirect Location header."""
    if "token=" not in location:
        raise AuthError("Moodle launch redirect did not contain a mobile token.")
    encoded = location.split("token=", 1)[1].split("&", 1)[0].split("#", 1)[0].strip()
    encoded = unquote(encoded)
    padding = "=" * (-len(encoded) % 4)
    try:
        decoded = base64.b64decode(encoded + padding).decode("utf-8")
    except Exception as exc:
        raise AuthError("Could not decode the Moodle mobile token payload.") from exc
    parts = decoded.split(":::")
    if len(parts) < 2 or not parts[1].strip():
        raise AuthError("Unexpected Moodle mobile token format.")
    return parts[1].strip()


def extract_cookie_value(raw: str) -> str:
    """Accept a bare cookie value, 'MoodleSession2027=...', or a copied Cookie/cURL string."""
    text = raw.strip()
    if not text:
        raise AuthError("Empty cookie value.")
    match = COOKIE_PAIR_RE.search(text)
    if match:
        return match.group(1).strip()
    if "=" in text and " " not in text:
        return text.split("=", 1)[1].strip()
    return text


def verify_token(year: int, token: str, base_url: str = DEFAULT_BASE_URL) -> dict[str, Any]:
    """Verify a wstoken against Moodle and return core_webservice_get_site_info."""
    site = Site(url=f"{base_url.rstrip('/')}/{year}", token=token.strip(), year=year)
    return get_site_info(site=site)


def exchange_cookie_for_token(
    cookie_input: str,
    year: int,
    base_url: str = DEFAULT_BASE_URL,
) -> tuple[str, dict[str, Any]]:
    """Exchange a browser MoodleSession<year> cookie for a long-lived Moodle mobile wstoken."""
    cookie_value = extract_cookie_value(cookie_input)
    cookie_name = f"MoodleSession{year}"
    url = launch_url(year=year, base_url=base_url)

    with httpx.Client(
        timeout=30,
        cookies={cookie_name: cookie_value, "MoodleSession": cookie_value},
        follow_redirects=False,
    ) as client:
        response = client.get(url)
    location = response.headers.get("location", "")
    if response.status_code in (301, 302, 303, 307, 308) and location.startswith("moodlemobile://"):
        token = parse_mobile_redirect(location)
        info = verify_token(year=year, token=token, base_url=base_url)
        return token, info

    if response.status_code in (301, 302, 303, 307, 308) and "login" in location.lower():
        raise AuthError(
            f"The {cookie_name} cookie was rejected or expired (Moodle redirected to login). "
            f"Make sure you are signed in to {base_url.rstrip('/')}/{year} and copy a fresh {cookie_name} cookie."
        )

    raise AuthError(
        f"Unexpected response from Moodle launch endpoint (HTTP {response.status_code}). "
        f"Make sure {cookie_name} is valid for {base_url.rstrip('/')}/{year}."
    )


class _FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[dict[str, Any]] = []
        self._current: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attr_map = {k.lower(): (v or "") for k, v in attrs}
        attr_names = {k.lower() for k, _ in attrs}
        if tag == "form":
            self._current = {
                "action": attr_map.get("action", ""),
                "method": attr_map.get("method", "get").strip().lower(),
                "inputs": [],
            }
            self.forms.append(self._current)
        elif self._current is not None and tag == "input":
            self._current["inputs"].append(
                {
                    "name": attr_map.get("name", ""),
                    "value": attr_map.get("value", ""),
                    "type": attr_map.get("type", "text").lower(),
                    "disabled": "disabled" in attr_names,
                    "checked": "checked" in attr_names,
                }
            )

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "form":
            self._current = None


def _parse_forms(html_text: str) -> list[dict[str, Any]]:
    parser = _FormParser()
    parser.feed(html_text)
    parser.close()
    return parser.forms


def _sanitize_url(url: str | httpx.URL) -> str:
    parsed = urlparse(str(url))
    if parsed.scheme and parsed.hostname:
        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        try:
            port_val = parsed.port
        except ValueError:
            port_val = None
        port = f":{port_val}" if port_val is not None else ""
        return f"{parsed.scheme}://{host}{port}{parsed.path}"
    return parsed.path or "<unknown>"


def _origin(url: str | httpx.URL) -> tuple[str, str, int]:
    parsed = urlparse(str(url))
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    try:
        port = parsed.port if parsed.port is not None else (443 if scheme == "https" else 80)
    except ValueError:
        port = -1
    return scheme, host, port


def _validate_base_url_for_credentials(base_url: str) -> tuple[str, str, int]:
    parsed = urlparse(base_url)
    if parsed.username is not None or parsed.password is not None or _origin(base_url) != RUNI_MOODLE_ORIGIN:
        raise AuthError(
            f"Credential login is only supported for {DEFAULT_BASE_URL} (got {_sanitize_url(base_url)}). "
            "Use `runi auth login --cookie` or `--token` for custom Moodle URLs."
        )
    return RUNI_MOODLE_ORIGIN


def _validate_trusted_url(
    url: str,
    *,
    allowed_origins: frozenset[tuple[str, str, int]],
    stage: str,
) -> str:
    parsed = urlparse(url)
    if parsed.username is not None or parsed.password is not None or _origin(url) not in allowed_origins:
        raise AuthError(
            f"Refusing to {stage} untrusted URL ({_sanitize_url(url)}). "
            "Only HTTPS (port 443) requests to trusted RUNI Moodle/SSO origins are allowed."
        )
    return url


def _is_moodle_launch_response(response: httpx.Response, moodle_origin: tuple[str, str, int]) -> bool:
    return _origin(response.url) == moodle_origin and urlparse(str(response.url)).path.endswith(
        "/admin/tool/mobile/launch.php"
    )


def _extract_mobile_link_from_html(response: httpx.Response, moodle_origin: tuple[str, str, int]) -> str | None:
    if response.status_code != 200 or not _is_moodle_launch_response(response, moodle_origin):
        return None
    match = MOBILE_TOKEN_LINK_RE.search(response.text)
    return match.group(0) if match else None


def _follow_http_redirects(
    client: httpx.Client,
    response: httpx.Response,
    *,
    moodle_origin: tuple[str, str, int],
    allowed_origins: frozenset[tuple[str, str, int]],
    max_hops: int = 15,
) -> tuple[httpx.Response, str | None]:
    for hop in range(max_hops + 1):
        if response.status_code in (307, 308):
            raise AuthError(
                f"Unsupported HTTP {response.status_code} redirect at {_sanitize_url(response.url)} during RUNI SSO."
            )
        if response.status_code not in (301, 302, 303):
            if response.status_code >= 400:
                raise AuthError(
                    f"RUNI SSO failed with HTTP {response.status_code} at {_sanitize_url(response.url)}."
                )
            return response, _extract_mobile_link_from_html(response, moodle_origin)

        location = response.headers.get("location", "").strip()
        if not location:
            raise AuthError(
                f"RUNI SSO returned HTTP {response.status_code} without a Location header at {_sanitize_url(response.url)}."
            )
        if location.lower().startswith("moodlemobile://"):
            if not _is_moodle_launch_response(response, moodle_origin):
                raise AuthError(
                    f"Refusing moodlemobile:// redirect from untrusted origin ({_sanitize_url(response.url)})."
                )
            return response, location

        if hop == max_hops:
            break

        next_url = urljoin(str(response.url), location)
        _validate_trusted_url(next_url, allowed_origins=allowed_origins, stage="follow redirect to")
        response = client.get(next_url)

    raise AuthError(
        f"Too many redirects during RUNI SSO (stopped at {_sanitize_url(response.url)})."
    )


def _find_form_with_fields(forms: list[dict[str, Any]], required_names: set[str]) -> dict[str, Any] | None:
    for form in forms:
        names = {inp["name"] for inp in form["inputs"] if inp["name"] and not inp.get("disabled")}
        if required_names.issubset(names):
            return form
    return None


def _form_data(form: dict[str, Any]) -> dict[str, str]:
    data: dict[str, str] = {}
    for inp in form["inputs"]:
        name = inp["name"]
        if not name or inp.get("disabled"):
            continue
        itype = inp.get("type", "text")
        if itype in {"submit", "button", "reset", "image", "file"}:
            continue
        if itype in {"checkbox", "radio"} and not inp.get("checked"):
            continue
        data[name] = inp["value"]
    return data


def _is_f5_auth_failure(response: httpx.Response, forms: list[dict[str, Any]]) -> bool:
    parsed = urlparse(str(response.url))
    if parsed.path.endswith("/my.logout.php3") or "errorcode=" in parsed.query.lower():
        return True
    return _find_form_with_fields(forms, {"username", "password"}) is not None


def login_with_credentials(
    username: str,
    password: str,
    year: int,
    base_url: str = DEFAULT_BASE_URL,
) -> tuple[str, dict[str, Any]]:
    """Sign in via RUNI SAML2 / F5 BIG-IP APM over pure HTTP and return a long-lived mobile wstoken.

    Credentials are used only in memory for this request and are never stored.
    """
    username = username.strip()
    if not username or not password:
        raise AuthError("Username and password are required.")

    moodle_origin = _validate_base_url_for_credentials(base_url)
    start_url = launch_url(year=year, base_url=base_url)
    headers = {"User-Agent": "Mozilla/5.0 (compatible; runi-moodle-cli)"}

    def submit_saml_forms(
        client: httpx.Client,
        resp: httpx.Response,
        max_steps: int = 5,
    ) -> tuple[httpx.Response, str | None]:
        for _ in range(max_steps):
            forms = _parse_forms(resp.text)
            saml_form = _find_form_with_fields(forms, {"SAMLResponse"}) or _find_form_with_fields(
                forms, {"SAMLRequest"}
            )
            if saml_form is None:
                return resp, None
            if saml_form["method"] != "post":
                raise AuthError(
                    f"Unsupported SAML form method {saml_form['method']!r} at {_sanitize_url(resp.url)}."
                )
            saml_url = urljoin(str(resp.url), saml_form["action"]) if saml_form["action"] else str(resp.url)
            _validate_trusted_url(saml_url, allowed_origins=RUNI_SSO_ORIGINS, stage="submit SAML form to")
            resp = client.post(saml_url, data=_form_data(saml_form))
            resp, mobile_loc = _follow_http_redirects(
                client,
                resp,
                moodle_origin=moodle_origin,
                allowed_origins=RUNI_SSO_ORIGINS,
            )
            if mobile_loc:
                return resp, mobile_loc
        return resp, None

    with httpx.Client(timeout=30, follow_redirects=False, headers=headers) as client:
        resp = client.get(start_url)
        resp, mobile_loc = _follow_http_redirects(
            client,
            resp,
            moodle_origin=moodle_origin,
            allowed_origins=RUNI_SSO_ORIGINS,
        )
        if mobile_loc:
            token = parse_mobile_redirect(mobile_loc)
            return token, verify_token(year=year, token=token, base_url=base_url)

        # Handle any pre-login SAMLRequest HTTP-POST binding forms before the credential page.
        resp, mobile_loc = submit_saml_forms(client, resp)
        if mobile_loc:
            token = parse_mobile_redirect(mobile_loc)
            return token, verify_token(year=year, token=token, base_url=base_url)

        forms = _parse_forms(resp.text)
        login_form = _find_form_with_fields(forms, {"username", "password"})
        if login_form is None:
            raise AuthError(
                f"Expected RUNI SSO login form at {_sanitize_url(resp.url)} (HTTP {resp.status_code}), "
                "but none was found. Try `runi auth login --cookie` or `runi auth login --browser`."
            )
        if login_form["method"] != "post":
            raise AuthError(
                f"Refusing to submit credentials using non-POST form at {_sanitize_url(resp.url)}."
            )

        payload = _form_data(login_form)
        payload["username"] = username
        payload["password"] = password
        post_url = urljoin(str(resp.url), login_form["action"]) if login_form["action"] else str(resp.url)
        _validate_trusted_url(post_url, allowed_origins=RUNI_CREDENTIAL_ORIGINS, stage="submit credentials to")

        resp = client.post(post_url, data=payload)
        resp, mobile_loc = _follow_http_redirects(
            client,
            resp,
            moodle_origin=moodle_origin,
            allowed_origins=RUNI_SSO_ORIGINS,
        )
        if mobile_loc:
            token = parse_mobile_redirect(mobile_loc)
            return token, verify_token(year=year, token=token, base_url=base_url)

        resp, mobile_loc = submit_saml_forms(client, resp)
        if mobile_loc:
            token = parse_mobile_redirect(mobile_loc)
            return token, verify_token(year=year, token=token, base_url=base_url)

        if _is_f5_auth_failure(resp, _parse_forms(resp.text)):
            raise AuthError("Invalid RUNI username or password.")

    raise AuthError(
        f"RUNI SSO did not complete (stopped at {_sanitize_url(resp.url)}, HTTP {resp.status_code}). "
        "If your account requires MFA or an extra verification step, use `runi auth login --cookie` or `--browser`."
    )


def login_with_browser(
    year: int,
    base_url: str = DEFAULT_BASE_URL,
    timeout_seconds: int = 300,
) -> tuple[str, dict[str, Any]]:
    """Open a real browser window via Playwright so the user can complete RUNI SSO,
    then capture the long-lived Moodle mobile wstoken automatically."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise AuthError(
            "Browser login requires Playwright. Install it with:\n"
            '  uv tool install "runi-moodle-cli[browser]"   (or: pip install "runi-moodle-cli[browser]")\n'
            "  playwright install chromium\n"
            "Or use `runi auth login --cookie` / `runi auth login --token` instead."
        ) from exc

    target_url = launch_url(year=year, base_url=base_url)
    captured_location: dict[str, str] = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()

        def on_response(response: Any) -> None:
            if "admin/tool/mobile/launch.php" in response.url:
                loc = response.headers.get("location", "")
                if loc.startswith("moodlemobile://"):
                    captured_location["value"] = loc

        page.on("response", on_response)
        try:
            page.goto(target_url, wait_until="domcontentloaded", timeout=30_000)
        except Exception:
            # Some browsers abort navigation when hitting a custom moodlemobile:// scheme
            pass

        elapsed_ms = 0
        step_ms = 500
        limit_ms = timeout_seconds * 1000
        while "value" not in captured_location and elapsed_ms < limit_ms:
            if page.is_closed():
                break
            page.wait_for_timeout(step_ms)
            elapsed_ms += step_ms

        browser.close()

    if "value" not in captured_location:
        raise AuthError(
            "Timed out or browser was closed before Moodle returned a mobile token. "
            "Please try `runi auth login --browser` again."
        )

    token = parse_mobile_redirect(captured_location["value"])
    info = verify_token(year=year, token=token, base_url=base_url)
    return token, info
