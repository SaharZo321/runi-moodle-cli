"""Cross-platform configuration and token storage (Windows, macOS, Linux).

Loads lazily from the user's config file (via platformdirs) with optional
overrides from environment variables.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from platformdirs import user_config_dir, user_data_dir

APP_NAME = "moodle-cli"
DEFAULT_BASE_URL = "https://moodle.runi.ac.il"
DEFAULT_LANG = "en"
LANG_CODE_RE = re.compile(r"^[a-z]{2,3}(?:_[a-z0-9]{1,8}){0,3}$")

_lang_override: str | None = None


class AuthError(RuntimeError):
    """Raised when Moodle authentication is missing or invalid."""


def normalize_base_url(base_url: str) -> str:
    cleaned = (base_url or "").strip().rstrip("/")
    parsed = urlparse(cleaned)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise AuthError("Moodle URL must be a valid https:// URL without credentials, query, or fragment.")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise AuthError("Moodle URL has an invalid port.") from exc
    return cleaned


def _url_origin(url: str) -> tuple[str, str, int]:
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    port = parsed.port if parsed.port is not None else (443 if scheme == "https" else 80)
    return scheme, host, port


def normalize_lang(lang: str) -> str:
    value = (lang or "").strip().lower()
    if value == "auto" or LANG_CODE_RE.fullmatch(value):
        return value
    raise ValueError(
        f"Unsupported language {lang!r}. Pass 'auto' or a Moodle language code (e.g. en, he, ar, de, fr, es)."
    )


def set_lang_override(lang: str | None) -> None:
    global _lang_override
    _lang_override = normalize_lang(lang) if lang is not None else None


@dataclass
class Site:
    url: str
    token: str
    year: int
    lang: str | None = DEFAULT_LANG


@dataclass
class Config:
    base_url: str = DEFAULT_BASE_URL
    default_year: int | None = None
    tokens: dict[int, str] = field(default_factory=dict)
    site_urls: dict[int, str] = field(default_factory=dict)
    default_site_url: str | None = None
    lang: str = DEFAULT_LANG
    env_site_url: str | None = None


def current_academic_year(today: date | None = None) -> int:
    """Reichman academic year number: Oct 2026 - Sep 2027 is 2027."""
    today = today or date.today()
    return today.year + 1 if today.month >= 10 else today.year


def config_path() -> Path:
    override = os.environ.get("MOODLE_CLI_CONFIG")
    if override:
        return Path(override).expanduser()
    return Path(user_config_dir(APP_NAME)) / "config.json"


def data_dir() -> Path:
    override = os.environ.get("MOODLE_CLI_DATA_DIR")
    if override:
        return Path(override).expanduser()
    return Path(user_data_dir(APP_NAME))


def _load_file_config(path: Path) -> Config:
    if not path.exists():
        return Config()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise AuthError(f"Could not read config file {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise AuthError(f"Invalid config file format in {path}.")
    tokens: dict[int, str] = {}
    for k, v in (raw.get("tokens") or {}).items():
        if v:
            try:
                tokens[int(k)] = str(v)
            except (TypeError, ValueError):
                continue
    site_urls: dict[int, str] = {}
    raw_site_urls = raw.get("site_urls")
    if isinstance(raw_site_urls, dict):
        for k, v in raw_site_urls.items():
            if v:
                try:
                    site_urls[int(k)] = normalize_base_url(str(v))
                except (TypeError, ValueError, AuthError):
                    continue
    default_year = raw.get("default_year")
    raw_default_site = raw.get("default_site_url")
    default_site_url: str | None = None
    if raw_default_site:
        try:
            default_site_url = normalize_base_url(str(raw_default_site))
        except (TypeError, ValueError, AuthError):
            default_site_url = None
    raw_lang = raw.get("lang")
    lang = normalize_lang(str(raw_lang)) if raw_lang else DEFAULT_LANG
    base_url = normalize_base_url(str(raw.get("base_url") or DEFAULT_BASE_URL))
    return Config(
        base_url=base_url,
        default_year=int(default_year) if default_year is not None else None,
        tokens=tokens,
        site_urls=site_urls,
        default_site_url=default_site_url,
        lang=lang,
    )


def site_url_for_year(cfg: Config, year: int) -> str:
    return cfg.site_urls.get(year) or cfg.env_site_url or f"{cfg.base_url}/{year}"


def _format_origin_url(url: str) -> str:
    parsed = urlparse(url)
    host = f"[{parsed.hostname}]" if parsed.hostname and ":" in parsed.hostname else (parsed.hostname or "")
    port_part = f":{parsed.port}" if parsed.port is not None else ""
    return f"{parsed.scheme.lower()}://{host}{port_part}"


def load_config() -> Config:
    """Load config from disk, then apply environment variable overrides."""
    cfg = _load_file_config(config_path())

    if env_site := os.environ.get("MOODLE_SITE_URL"):
        new_site = normalize_base_url(env_site)
        # Only retain file tokens already bound to this exact site URL (check BEFORE setting env_site_url).
        keep = {y for y in cfg.tokens if site_url_for_year(cfg, y) == new_site}
        cfg.env_site_url = new_site
        cfg.default_site_url = new_site
        cfg.tokens = {y: t for y, t in cfg.tokens.items() if y in keep}
        cfg.site_urls = {y: new_site for y in keep}
        if cfg.default_year not in cfg.tokens:
            cfg.default_year = None
    elif env_base := os.environ.get("MOODLE_BASE_URL"):
        new_base = normalize_base_url(env_base)
        # Only retain file tokens whose resolved URL matches the overridden base_url/<year>.
        keep = {y for y in cfg.tokens if site_url_for_year(cfg, y) == f"{new_base}/{y}"}
        cfg.tokens = {y: t for y, t in cfg.tokens.items() if y in keep}
        cfg.site_urls.clear()
        cfg.default_site_url = None
        if cfg.default_year not in cfg.tokens:
            cfg.default_year = None
        cfg.base_url = new_base

    for key, value in os.environ.items():
        if key.startswith("MOODLE_TOKEN_") and value:
            suffix = key.removeprefix("MOODLE_TOKEN_")
            if suffix.isdigit():
                y = int(suffix)
                cfg.tokens[y] = value
                if cfg.env_site_url is not None:
                    cfg.site_urls[y] = cfg.env_site_url

    if env_year := os.environ.get("MOODLE_DEFAULT_YEAR"):
        if env_year.isdigit():
            cfg.default_year = int(env_year)

    if env_lang := os.environ.get("MOODLE_LANG"):
        cfg.lang = normalize_lang(env_lang)

    if cfg.default_year is None and cfg.tokens:
        cfg.default_year = max(cfg.tokens)

    return cfg


def atomic_write_text(path: Path, text: str, *, private: bool = False) -> Path:
    """Atomically write UTF-8 text via a temporary file in the same directory."""
    existed = path.parent.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    if private and not existed and os.name != "nt":
        os.chmod(path.parent, 0o700)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        if private and os.name != "nt":
            try:
                os.fchmod(fd, 0o600)
            except Exception:
                os.close(fd)
                raise
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return path


def save_config(cfg: Config) -> Path:
    """Atomically save config to disk with 0600 permissions on POSIX systems."""
    cfg.base_url = normalize_base_url(cfg.base_url)
    path = config_path()
    payload: dict[str, object] = {
        "base_url": cfg.base_url,
        "default_year": cfg.default_year,
        "lang": cfg.lang,
        "tokens": {str(year): token for year, token in sorted(cfg.tokens.items())},
    }
    if cfg.site_urls:
        payload["site_urls"] = {
            str(year): normalize_base_url(url) for year, url in sorted(cfg.site_urls.items())
        }
    if cfg.default_site_url:
        payload["default_site_url"] = normalize_base_url(cfg.default_site_url)
    return atomic_write_text(path, json.dumps(payload, indent=2) + "\n", private=True)


def check_origin_conflict(*, base_url: str | None = None, site_url: str | None = None) -> None:
    """Raise AuthError before prompting if config already holds tokens for a different origin."""
    target_origin_url = site_url or base_url
    if not target_origin_url:
        return
    normalized_target = normalize_base_url(target_origin_url)
    cfg = _load_file_config(config_path())
    if cfg.tokens and _url_origin(normalized_target) != _url_origin(cfg.base_url):
        raise AuthError(
            f"Config already holds tokens for {cfg.base_url}. "
            "Run `mdl auth logout --all` (or set MOODLE_CLI_CONFIG to a separate file) "
            f"before signing in to {normalized_target}."
        )


def save_token(
    year: int,
    token: str,
    base_url: str | None = None,
    set_default: bool = True,
    lang: str | None = None,
    site_url: str | None = None,
    set_default_site: bool = True,
) -> Path:
    """Persist a Moodle mobile token for a specific academic year (or exact site_url)."""
    check_origin_conflict(base_url=base_url, site_url=site_url)
    cfg = _load_file_config(config_path())
    target_origin_url = site_url or base_url
    if target_origin_url:
        normalized_target = normalize_base_url(target_origin_url)
        if _url_origin(normalized_target) != _url_origin(cfg.base_url):
            cfg.default_year = None
            cfg.default_site_url = None
            cfg.site_urls.clear()
        if site_url:
            if not cfg.tokens:
                cfg.base_url = _format_origin_url(normalized_target)
            cfg.site_urls[year] = normalized_target
            if set_default_site and (set_default or cfg.default_year is None):
                cfg.default_site_url = normalized_target
        else:
            if normalized_target != cfg.base_url:
                # Preserve existing untouched years' resolved URLs before changing shared base_url.
                for existing_year in cfg.tokens:
                    if existing_year != year and existing_year not in cfg.site_urls:
                        cfg.site_urls[existing_year] = f"{cfg.base_url}/{existing_year}"
            cfg.base_url = normalized_target
            cfg.site_urls.pop(year, None)
            if set_default or cfg.default_year is None:
                cfg.default_site_url = None
        if cfg.default_site_url and cfg.default_site_url not in cfg.site_urls.values():
            cfg.default_site_url = None
    if lang is not None:
        cfg.lang = normalize_lang(lang)
    cfg.tokens[year] = token.strip()
    if set_default or cfg.default_year is None:
        cfg.default_year = year
    return save_config(cfg)


def remove_token(year: int | None = None) -> Path:
    """Remove one year's token, or all tokens if year is None."""
    cfg = _load_file_config(config_path())
    if year is None:
        cfg.tokens.clear()
        cfg.site_urls.clear()
        cfg.default_year = None
        cfg.default_site_url = None
        cfg.base_url = DEFAULT_BASE_URL
    else:
        was_default = cfg.default_year == year
        cfg.tokens.pop(year, None)
        cfg.site_urls.pop(year, None)
        if was_default:
            cfg.default_year = max(cfg.tokens) if cfg.tokens else None
        if cfg.default_site_url and cfg.default_site_url not in cfg.site_urls.values():
            cfg.default_site_url = None
        if not cfg.tokens:
            cfg.default_site_url = None
            cfg.base_url = DEFAULT_BASE_URL
    return save_config(cfg)


def get_site(year: int | None = None) -> Site:
    cfg = load_config()
    target_year = year if year is not None else cfg.default_year
    if target_year is None:
        suggested = current_academic_year()
        raise AuthError(
            f"Not signed in to Moodle. Run `mdl auth login --year {suggested}` to sign in."
        )
    if target_year not in cfg.tokens:
        raise AuthError(
            f"No Moodle token configured for {target_year}. "
            f"Run `mdl auth login --year {target_year}` to sign in."
        )
    effective_lang = _lang_override if _lang_override is not None else cfg.lang
    api_lang = None if effective_lang == "auto" else effective_lang
    return Site(
        url=site_url_for_year(cfg, target_year),
        token=cfg.tokens[target_year],
        year=target_year,
        lang=api_lang,
    )


def configured_years() -> list[int]:
    return sorted(load_config().tokens)
