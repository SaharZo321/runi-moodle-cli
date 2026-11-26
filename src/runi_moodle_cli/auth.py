"""Authentication helpers for obtaining and verifying long-lived Moodle Mobile tokens."""

from __future__ import annotations

import base64
import re
import secrets
from typing import Any
from urllib.parse import urlencode

import httpx

from runi_moodle_cli.client import get_site_info
from runi_moodle_cli.config import AuthError, DEFAULT_BASE_URL, Site

COOKIE_PAIR_RE = re.compile(r"MoodleSession\d*=([^;\s'\"]+)", re.I)


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
    encoded = location.split("token=", 1)[1].split("&", 1)[0].strip()
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
