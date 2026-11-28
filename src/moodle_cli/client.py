"""Moodle REST Web Services API client."""

from __future__ import annotations

import atexit
import json
import logging
import mimetypes
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from moodle_cli.config import AuthError, Site, get_site

logging.getLogger("httpx").setLevel(logging.WARNING)

_http = httpx.Client(timeout=30)
atexit.register(_http.close)
_user_id_cache: dict[tuple[str, str], int] = {}

_URL_SECRET_RE = re.compile(r"([?&](?:token|wstoken|password|pass)=)[^&\s'\"]+", re.I)


def redact_secrets(text: str, token: str | None = None) -> str:
    """Scrub URL query secrets and any literal bearer/ws token echoes from text."""
    redacted = _URL_SECRET_RE.sub(r"\1<redacted>", text)
    if token and len(token) >= 6:
        redacted = redacted.replace(token, "<redacted>")
    return redacted


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


def _flatten(params: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Flatten nested dicts and lists into Moodle's bracket form-encoding."""
    flat: dict[str, Any] = {}
    for key, value in params.items():
        full_key = f"{prefix}[{key}]" if prefix else key
        if isinstance(value, dict):
            flat.update(_flatten(value, full_key))
        elif isinstance(value, list):
            for i, item in enumerate(value):
                item_key = f"{full_key}[{i}]"
                if isinstance(item, dict):
                    flat.update(_flatten(item, item_key))
                elif isinstance(item, list):
                    flat.update(_flatten({str(j): v for j, v in enumerate(item)}, item_key))
                else:
                    flat[item_key] = item
        else:
            flat[full_key] = value
    return flat


def call_site(site: Site, wsfunction: str, *, retry: bool = True, **params: Any) -> Any:
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
    if retry and response.status_code >= 500:
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
        code = str(result.get("errorcode") or "moodle_error")
        msg = str(result.get("message", "Unknown Moodle error"))
        detail = redact_secrets(f"{code}: {msg}", site.token)
        if code == "invalidtoken":
            raise AuthError(detail)
        raise MoodleError(detail)
    return result


def call(wsfunction: str, year: int | None = None, **params: Any) -> Any:
    site = get_site(year)
    return call_site(site, wsfunction, **params)


def get_site_info(year: int | None = None, site: Site | None = None) -> dict[str, Any]:
    target = site or get_site(year)
    return call_site(target, "core_webservice_get_site_info")


def upload_draft_file(
    site: Site,
    path: Path,
    itemid: int = 0,
    filename: str | None = None,
) -> dict[str, Any]:
    """Upload a local file to a Moodle user draft area via webservice/upload.php."""
    url = f"{site.url}/webservice/upload.php"
    upload_name = filename or path.name
    mime_type = mimetypes.guess_type(upload_name)[0] or "application/octet-stream"
    with path.open("rb") as fh:
        response = _http.post(
            url,
            data={
                "token": site.token,
                "filearea": "draft",
                "itemid": str(itemid),
                "filepath": "/",
            },
            files={"file_0": (upload_name, fh, mime_type)},
            follow_redirects=False,
            timeout=120.0,
        )
    if response.is_redirect:
        raise MoodleError("Moodle redirected the file upload unexpectedly; verify your site URL and session.")
    response.raise_for_status()
    try:
        result = response.json()
    except json.JSONDecodeError as exc:
        raise MoodleError(
            f"Moodle returned a non-JSON response for file upload (HTTP {response.status_code})."
        ) from exc
    if isinstance(result, dict):
        code = str(result.get("errorcode") or result.get("errortype") or "upload_error")
        raw_msg = result.get("error") or result.get("message") or "File upload failed"
        detail = redact_secrets(f"{code}: {raw_msg}", site.token)
        if code == "invalidtoken":
            raise AuthError(detail)
        raise MoodleError(detail)
    if not isinstance(result, list) or not result or not isinstance(result[0], dict):
        raise MoodleError("Moodle file upload returned an unexpected response.")
    entry = result[0]
    if entry.get("error") or entry.get("errortype") or entry.get("exception"):
        code = str(entry.get("errorcode") or entry.get("errortype") or "upload_error")
        raw_msg = entry.get("error") or entry.get("message") or "File upload failed"
        detail = redact_secrets(f"{code}: {raw_msg}", site.token)
        if code == "invalidtoken":
            raise AuthError(detail)
        raise MoodleError(detail)
    try:
        returned_itemid = int(entry["itemid"])
    except (KeyError, TypeError, ValueError) as exc:
        raise MoodleError("Moodle file upload returned an unexpected response without a valid draft itemid.") from exc
    if returned_itemid <= 0:
        raise MoodleError("Moodle file upload returned an invalid draft itemid (<= 0).")
    if itemid > 0 and returned_itemid != itemid:
        raise MoodleError(
            f"Moodle file upload returned draft itemid {returned_itemid} instead of expected {itemid}."
        )
    return entry


def get_user_id(year: int | None = None) -> int:
    site = get_site(year)
    key = (site.url, site.token)
    if key not in _user_id_cache:
        _user_id_cache[key] = int(get_site_info(site=site)["userid"])
    return _user_id_cache[key]
