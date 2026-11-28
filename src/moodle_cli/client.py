"""Moodle REST Web Services API client."""

from __future__ import annotations

import atexit
import json
import logging
import re
import time
from typing import Any
from urllib.parse import urlparse

import httpx

from moodle_cli.config import AuthError, Site, get_site

logging.getLogger("httpx").setLevel(logging.WARNING)

_http = httpx.Client(timeout=30)
atexit.register(_http.close)
_user_id_cache: dict[tuple[str, str], int] = {}

_URL_SECRET_RE = re.compile(r"([?&](?:token|wstoken|password|pass)=)[^&\s'\"]+", re.I)


def sanitize_url(url: str | httpx.URL) -> str:
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


def sanitize_http_error(exc: httpx.HTTPError) -> str:
    """Format an httpx exception without leaking query-string tokens or credentials."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code} for {sanitize_url(exc.request.url)}"
    req = getattr(exc, "request", None)
    if req is not None:
        return f"{type(exc).__name__} for {sanitize_url(req.url)}"
    return _URL_SECRET_RE.sub(r"\1<redacted>", str(exc))


class MoodleError(Exception):
    """Moodle returned an application-level error (often inside HTTP 200)."""


def _flatten(params: dict[str, Any]) -> dict[str, Any]:
    """{"courseids": [5, 7]} -> {"courseids[0]": 5, "courseids[1]": 7}."""
    flat: dict[str, Any] = {}
    for key, value in params.items():
        if isinstance(value, list):
            for i, item in enumerate(value):
                flat[f"{key}[{i}]"] = item
        else:
            flat[key] = value
    return flat


def call_site(site: Site, wsfunction: str, **params: Any) -> Any:
    data = {
        "wstoken": site.token,
        "wsfunction": wsfunction,
        "moodlewsrestformat": "json",
        "moodlewssettingfilter": "true",
    }
    if site.lang:
        data["moodlewssettinglang"] = site.lang
    data.update(_flatten(params))
    url = f"{site.url}/webservice/rest/server.php"
    response = _http.post(url, data=data)
    if response.status_code >= 500:
        time.sleep(1)
        response = _http.post(url, data=data)
    response.raise_for_status()
    try:
        result = response.json()
    except json.JSONDecodeError as exc:
        raise MoodleError(
            f"Moodle returned a non-JSON response for {wsfunction} (HTTP {response.status_code})."
        ) from exc
    if isinstance(result, dict) and "exception" in result:
        code = result.get("errorcode", "moodle_error")
        msg = _URL_SECRET_RE.sub(r"\1<redacted>", str(result.get("message", "Unknown Moodle error")))
        if code == "invalidtoken":
            raise AuthError(f"{code}: {msg}")
        raise MoodleError(f"{code}: {msg}")
    return result


def call(wsfunction: str, year: int | None = None, **params: Any) -> Any:
    site = get_site(year)
    return call_site(site, wsfunction, **params)


def get_site_info(year: int | None = None, site: Site | None = None) -> dict[str, Any]:
    target = site or get_site(year)
    return call_site(target, "core_webservice_get_site_info")


def get_user_id(year: int | None = None) -> int:
    site = get_site(year)
    key = (site.url, site.token)
    if key not in _user_id_cache:
        _user_id_cache[key] = int(get_site_info(site=site)["userid"])
    return _user_id_cache[key]
