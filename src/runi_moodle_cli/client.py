"""Moodle REST Web Services API client."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from runi_moodle_cli.config import AuthError, Site, get_site

logging.getLogger("httpx").setLevel(logging.WARNING)

_http = httpx.Client(timeout=30)
_user_id_cache: dict[tuple[str, str], int] = {}


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
        **_flatten(params),
    }
    url = f"{site.url}/webservice/rest/server.php"
    response = _http.post(url, data=data)
    if response.status_code >= 500:
        time.sleep(1)
        response = _http.post(url, data=data)
    response.raise_for_status()
    result = response.json()
    if isinstance(result, dict) and "exception" in result:
        code = result.get("errorcode", "moodle_error")
        msg = result.get("message", "Unknown Moodle error")
        if code in {"invalidtoken", "accessexception"}:
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
