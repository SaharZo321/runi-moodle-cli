"""Authentication helpers for obtaining and verifying long-lived Moodle Mobile tokens."""

from __future__ import annotations

import base64
from html.parser import HTMLParser
import re
import secrets
import sys
from typing import Any
from urllib.parse import unquote, urlencode, urljoin, urlparse

import httpx

from moodle_cli.client import get_site_info, sanitize_url
from moodle_cli.config import AuthError, DEFAULT_BASE_URL, Site, normalize_base_url

COOKIE_PAIR_RE = re.compile(r"\b(MoodleSession[A-Za-z0-9_-]*)=([^;\s'\"]+)", re.I)
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


def resolve_site_url(year: int, base_url: str = DEFAULT_BASE_URL, site_url: str | None = None) -> str:
    if site_url is not None:
        return normalize_base_url(site_url)
    return f"{normalize_base_url(base_url)}/{year}"


def launch_url(
    year: int,
    base_url: str = DEFAULT_BASE_URL,
    passport: str | None = None,
    *,
    site_url: str | None = None,
) -> str:
    passport = passport or secrets.token_hex(8)
    query = urlencode(
        {
            "service": "moodle_mobile_app",
            "passport": passport,
            "urlscheme": "moodlemobile",
        }
    )
    resolved = resolve_site_url(year, base_url=base_url, site_url=site_url)
    return f"{resolved}/admin/tool/mobile/launch.php?{query}"


def parse_mobile_redirect(location: str) -> str:
    """Extract the wstoken from a moodlemobile://token=<base64> redirect Location header."""
    if "token=" not in location:
        raise AuthError("Moodle launch redirect did not contain a mobile token.")
    encoded = location.split("token=", 1)[1].split("&", 1)[0].split("#", 1)[0].strip()
    encoded = unquote(encoded)
    padding = "=" * (-len(encoded) % 4)
    try:
        decoded = base64.urlsafe_b64decode(encoded + padding).decode("utf-8")
    except Exception as exc:
        raise AuthError("Could not decode the Moodle mobile token payload.") from exc
    parts = decoded.split(":::")
    if len(parts) < 2 or not parts[1].strip():
        raise AuthError("Unexpected Moodle mobile token format.")
    return parts[1].strip()


def extract_cookie_pair(raw: str, year: int | None = None) -> tuple[str | None, str]:
    """Return (cookie_name_or_None, cookie_value) from a bare value or Cookie header."""
    text = raw.strip()
    if not text:
        raise AuthError("Empty cookie value.")
    if any(ch in text for ch in ("\r", "\n", "\x00")):
        raise AuthError("Invalid control character in cookie value.")
    if year is not None:
        year_match = re.search(rf"\b(MoodleSession{year})=([^;\s'\"]+)", text, re.I)
        if year_match:
            return year_match.group(1), year_match.group(2).strip()
    exact_match = re.search(r"\b(MoodleSession)=([^;\s'\"]+)", text, re.I)
    if exact_match:
        return exact_match.group(1), exact_match.group(2).strip()
    saw_test_cookie = False
    for match in COOKIE_PAIR_RE.finditer(text):
        name, val = match.group(1), match.group(2).strip()
        if name.lower().startswith("moodlesessiontest"):
            saw_test_cookie = True
            continue
        return name, val
    if saw_test_cookie:
        raise AuthError("MoodleSessionTest is a test cookie, not your Moodle session cookie.")
    if "=" in text and " " not in text:
        name, val = text.split("=", 1)
        name = name.strip()
        if name.lower().startswith("moodlesessiontest"):
            raise AuthError("MoodleSessionTest is a test cookie, not your Moodle session cookie.")
        val = val.rstrip(";").strip()
        if not val:
            raise AuthError("Empty cookie value.")
        return name or None, val
    cleaned = text.rstrip(";").strip()
    if not cleaned:
        raise AuthError("Empty cookie value.")
    return None, cleaned


def extract_cookie_value(raw: str, year: int | None = None) -> str:
    """Accept a bare cookie value, 'MoodleSession2027=...', or a copied Cookie/cURL string."""
    return extract_cookie_pair(raw, year=year)[1]


def verify_token(
    year: int,
    token: str,
    base_url: str = DEFAULT_BASE_URL,
    *,
    site_url: str | None = None,
) -> dict[str, Any]:
    """Verify a wstoken against Moodle and return core_webservice_get_site_info."""
    resolved = resolve_site_url(year, base_url=base_url, site_url=site_url)
    site = Site(url=resolved, token=token.strip(), year=year)
    return get_site_info(site=site)


def exchange_cookie_for_token(
    cookie_input: str,
    year: int,
    base_url: str = DEFAULT_BASE_URL,
    *,
    site_url: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Exchange a browser MoodleSession cookie for a long-lived Moodle mobile wstoken."""
    resolved = resolve_site_url(year, base_url=base_url, site_url=site_url)
    parsed_name, cookie_value = extract_cookie_pair(cookie_input, year=year)
    cookie_name = parsed_name or (f"MoodleSession{year}" if site_url is None else "MoodleSession")
    cookies = {cookie_name: cookie_value, "MoodleSession": cookie_value, f"MoodleSession{year}": cookie_value}
    url = launch_url(year=year, base_url=base_url, site_url=site_url)

    with httpx.Client(
        timeout=30,
        cookies=cookies,
        follow_redirects=False,
    ) as client:
        response = client.get(url)
    location = response.headers.get("location", "")
    if response.status_code in (301, 302, 303, 307, 308) and location.lower().startswith("moodlemobile://"):
        token = parse_mobile_redirect(location)
        info = verify_token(year=year, token=token, base_url=base_url, site_url=site_url)
        return token, info

    if response.status_code in (301, 302, 303, 307, 308) and "login" in location.lower():
        raise AuthError(
            f"The {cookie_name} cookie was rejected or expired (Moodle redirected to login). "
            f"Make sure you are signed in to {resolved} and copy a fresh session cookie."
        )

    raise AuthError(
        f"Unexpected response from Moodle launch endpoint (HTTP {response.status_code}). "
        f"Make sure {cookie_name} is valid for {resolved}."
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
    return sanitize_url(url)


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
            f"Year-based --base-url credential login is only supported for {DEFAULT_BASE_URL} "
            f"(got {_sanitize_url(base_url)}). "
            "Use `--site <https://...>` for other Moodle sites, or `--browser` / `--cookie` / `--token`."
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


def _login_with_moodle_token_endpoint(
    username: str,
    password: str,
    year: int,
    resolved_site: str,
) -> tuple[str, dict[str, Any]]:
    """Sign in to a standard Moodle site via /login/token.php over HTTPS (no redirects followed)."""
    token_url = f"{resolved_site}/login/token.php"
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        resp = client.post(
            token_url,
            data={"username": username, "password": password, "service": "moodle_mobile_app"},
        )
    if resp.status_code != 200:
        raise AuthError(
            f"Direct credential login is not supported by {resolved_site} (HTTP {resp.status_code}). "
            "Use `mdl auth login --browser`, `--cookie`, or `--token` instead."
        )
    try:
        payload = resp.json()
    except ValueError as exc:
        raise AuthError(
            f"{resolved_site} did not return a JSON token response (it likely requires SSO). "
            "Use `mdl auth login --browser`, `--cookie`, or `--token` instead."
        ) from exc
    if isinstance(payload, dict) and isinstance(payload.get("token"), str) and payload["token"].strip():
        token = payload["token"].strip()
        info = verify_token(year=year, token=token, site_url=resolved_site)
        return token, info
    code = str(payload.get("errorcode") or "").lower() if isinstance(payload, dict) else ""
    if code == "invalidlogin":
        raise AuthError("Invalid Moodle username or password.")
    if code in {"servicenotavailable", "enablewsdescription"}:
        raise AuthError(
            f"The Moodle Mobile App web service is not enabled on {resolved_site}."
        )
    raise AuthError(
        f"Direct credential login was rejected by {resolved_site}. "
        "If this site uses university SSO, use `mdl auth login --browser`, `--cookie`, or `--token`."
    )


def login_with_credentials(
    username: str,
    password: str,
    year: int,
    base_url: str = DEFAULT_BASE_URL,
    *,
    site_url: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Sign in with username/password over HTTPS and return a long-lived mobile wstoken.

    Uses pinned F5 BIG-IP APM + SAML2 for Reichman University (moodle.runi.ac.il),
    and Moodle's standard /login/token.php endpoint for other HTTPS Moodle sites.
    Credentials are used only in memory for this request and are never stored.
    """
    username = username.strip()
    if not username or not password:
        raise AuthError("Username and password are required.")

    resolved_site = resolve_site_url(year, base_url=base_url, site_url=site_url)
    if _origin(resolved_site) != RUNI_MOODLE_ORIGIN:
        if site_url is None:
            # --base-url credential login is RUNI-only; non-RUNI sites must use --site.
            _validate_base_url_for_credentials(base_url)
        return _login_with_moodle_token_endpoint(username, password, year, resolved_site)

    moodle_origin = _validate_base_url_for_credentials(resolved_site if site_url else base_url)
    start_url = launch_url(year=year, base_url=base_url, site_url=site_url)
    headers = {"User-Agent": "Mozilla/5.0 (compatible; moodle-cli)"}

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
            return token, verify_token(year=year, token=token, base_url=base_url, site_url=site_url)

        # Handle any pre-login SAMLRequest HTTP-POST binding forms before the credential page.
        resp, mobile_loc = submit_saml_forms(client, resp)
        if mobile_loc:
            token = parse_mobile_redirect(mobile_loc)
            return token, verify_token(year=year, token=token, base_url=base_url, site_url=site_url)

        forms = _parse_forms(resp.text)
        login_form = _find_form_with_fields(forms, {"username", "password"})
        if login_form is None:
            raise AuthError(
                f"Expected RUNI SSO login form at {_sanitize_url(resp.url)} (HTTP {resp.status_code}), "
                "but none was found. Try `mdl auth login --cookie` or `mdl auth login --browser`."
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
            return token, verify_token(year=year, token=token, base_url=base_url, site_url=site_url)

        resp, mobile_loc = submit_saml_forms(client, resp)
        if mobile_loc:
            token = parse_mobile_redirect(mobile_loc)
            return token, verify_token(year=year, token=token, base_url=base_url, site_url=site_url)

        if _is_f5_auth_failure(resp, _parse_forms(resp.text)):
            raise AuthError("Invalid RUNI username or password.")

    raise AuthError(
        f"RUNI SSO did not complete (stopped at {_sanitize_url(resp.url)}, HTTP {resp.status_code}). "
        "If your account requires MFA or an extra verification step, use `mdl auth login --cookie` or `--browser`."
    )


_BROWSER_CHANNELS: tuple[str | None, ...] = ("chrome", "msedge", None)


def _launch_browser(playwright: Any) -> Any:
    """Launch installed Chrome or Edge first, falling back to Playwright's bundled Chromium."""
    last_exc: Exception | None = None
    for channel in _BROWSER_CHANNELS:
        try:
            if channel is None:
                return playwright.chromium.launch(headless=False)
            return playwright.chromium.launch(headless=False, channel=channel)
        except Exception as exc:
            last_exc = exc
    last_detail = str(last_exc).strip().splitlines()[0].strip() if last_exc and str(last_exc).strip() else ""
    detail_line = f"\nLast error: {last_detail}" if last_detail else ""
    install_cmd = (
        f'& "{sys.executable}" -m playwright install chromium'
        if sys.platform == "win32"
        else f'"{sys.executable}" -m playwright install chromium'
    )
    raise AuthError(
        "Could not launch Google Chrome, Microsoft Edge, or Playwright Chromium."
        f"{detail_line}\n"
        "Install Chrome or Edge (no extra download needed), or download Playwright's Chromium with:\n"
        f"  {install_cmd}\n"
        "Or use `mdl auth login` (interactive credentials) or `--cookie` / `--token` instead."
    ) from last_exc


def login_with_browser(
    year: int,
    base_url: str = DEFAULT_BASE_URL,
    timeout_seconds: int = 300,
    *,
    site_url: str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Open a real browser window via Playwright so the user can complete SSO,
    then capture the long-lived Moodle mobile wstoken automatically."""
    resolved = resolve_site_url(year, base_url=base_url, site_url=site_url)
    moodle_origin = _origin(resolved)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        if getattr(sys, "frozen", False):
            raise AuthError(
                "Browser SSO login (--browser) is not bundled in the standalone binary.\n"
                "Use `mdl auth login` (interactive credentials), `--cookie`, or `--token` instead,\n"
                "or install the Python package with browser support and run that `mdl`:\n"
                '  uv tool install "moodle-cli[browser] @ git+https://github.com/SaharZo321/moodle-cli.git"'
            ) from exc
        raise AuthError(
            "Browser login requires Playwright. Install it with:\n"
            '  uv tool install "moodle-cli[browser] @ git+https://github.com/SaharZo321/moodle-cli.git"\n'
            "Uses your existing Chrome or Edge automatically (no browser download needed).\n"
            "Or use `mdl auth login` (interactive credentials) or `--cookie` / `--token` instead."
        ) from exc

    target_url = launch_url(year=year, base_url=base_url, site_url=site_url)
    captured_location: dict[str, str] = {}

    with sync_playwright() as p:
        browser = _launch_browser(p)
        try:
            context = browser.new_context()
            page = context.new_page()

            def on_response(response: Any) -> None:
                if _origin(response.url) == moodle_origin and urlparse(response.url).path.endswith(
                    "/admin/tool/mobile/launch.php"
                ):
                    loc = response.headers.get("location", "")
                    if loc.lower().startswith("moodlemobile://"):
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
        finally:
            browser.close()

    if "value" not in captured_location:
        raise AuthError(
            "Timed out or browser was closed before Moodle returned a mobile token. "
            "Please try `mdl auth login --browser` again."
        )

    token = parse_mobile_redirect(captured_location["value"])
    info = verify_token(year=year, token=token, base_url=base_url, site_url=site_url)
    return token, info
