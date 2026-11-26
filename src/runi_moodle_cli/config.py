"""Cross-platform configuration and token storage (Windows, macOS, Linux).

Loads lazily from the user's config file (via platformdirs) with optional
overrides from environment variables.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from platformdirs import user_config_dir, user_data_dir

APP_NAME = "runi-moodle-cli"
DEFAULT_BASE_URL = "https://moodle.runi.ac.il"
DEFAULT_LANG = "en"
VALID_LANGS = frozenset({"en", "he", "auto"})

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
    ):
        raise AuthError("Moodle base URL must be a valid https:// URL without embedded credentials.")
    try:
        _ = parsed.port
    except ValueError as exc:
        raise AuthError("Moodle base URL has an invalid port.") from exc
    return cleaned


def _url_origin(url: str) -> tuple[str, str, int]:
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    port = parsed.port if parsed.port is not None else (443 if scheme == "https" else 80)
    return scheme, host, port


def normalize_lang(lang: str) -> str:
    value = lang.strip().lower()
    if value not in VALID_LANGS:
        allowed = ", ".join(sorted(VALID_LANGS))
        raise ValueError(f"Unsupported language {lang!r}. Choose one of: {allowed}.")
    return value


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
    lang: str = DEFAULT_LANG


def current_academic_year(today: date | None = None) -> int:
    """Reichman academic year number: Oct 2026 - Sep 2027 is 2027."""
    today = today or date.today()
    return today.year + 1 if today.month >= 10 else today.year


def config_path() -> Path:
    override = os.environ.get("RUNI_MOODLE_CONFIG")
    if override:
        return Path(override).expanduser()
    return Path(user_config_dir(APP_NAME)) / "config.json"


def data_dir() -> Path:
    override = os.environ.get("RUNI_MOODLE_DATA_DIR")
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
    default_year = raw.get("default_year")
    raw_lang = raw.get("lang")
    lang = normalize_lang(str(raw_lang)) if raw_lang else DEFAULT_LANG
    base_url = normalize_base_url(str(raw.get("base_url") or DEFAULT_BASE_URL))
    return Config(
        base_url=base_url,
        default_year=int(default_year) if default_year is not None else None,
        tokens=tokens,
        lang=lang,
    )


def load_config() -> Config:
    """Load config from disk, then apply environment variable overrides."""
    cfg = _load_file_config(config_path())
    file_origin = _url_origin(cfg.base_url)

    if env_base := os.environ.get("MOODLE_BASE_URL"):
        new_base = normalize_base_url(env_base)
        if _url_origin(new_base) != file_origin:
            # Never send saved tokens from one origin to a different MOODLE_BASE_URL.
            cfg.tokens.clear()
            cfg.default_year = None
        cfg.base_url = new_base

    for key, value in os.environ.items():
        if key.startswith("MOODLE_TOKEN_") and value:
            suffix = key.removeprefix("MOODLE_TOKEN_")
            if suffix.isdigit():
                cfg.tokens[int(suffix)] = value

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
    payload = {
        "base_url": cfg.base_url,
        "default_year": cfg.default_year,
        "lang": cfg.lang,
        "tokens": {str(year): token for year, token in sorted(cfg.tokens.items())},
    }
    return atomic_write_text(path, json.dumps(payload, indent=2) + "\n", private=True)


def save_token(
    year: int,
    token: str,
    base_url: str | None = None,
    set_default: bool = True,
    lang: str | None = None,
) -> Path:
    """Persist a Moodle mobile token for a specific academic year."""
    cfg = _load_file_config(config_path())
    if base_url:
        normalized = normalize_base_url(base_url)
        if _url_origin(normalized) != _url_origin(cfg.base_url):
            if cfg.tokens:
                raise AuthError(
                    f"Config already holds tokens for {cfg.base_url}. "
                    "Run `runi auth logout --all` (or set RUNI_MOODLE_CONFIG to a separate file) "
                    f"before signing in to {normalized}."
                )
            cfg.default_year = None
        cfg.base_url = normalized
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
        cfg.default_year = None
    else:
        cfg.tokens.pop(year, None)
        if cfg.default_year == year:
            cfg.default_year = max(cfg.tokens) if cfg.tokens else None
    return save_config(cfg)


def get_site(year: int | None = None) -> Site:
    cfg = load_config()
    target_year = year if year is not None else cfg.default_year
    if target_year is None:
        suggested = current_academic_year()
        raise AuthError(
            f"Not signed in to Moodle. Run `runi auth login --year {suggested}` to sign in."
        )
    if target_year not in cfg.tokens:
        raise AuthError(
            f"No Moodle token configured for {target_year}. "
            f"Run `runi auth login --year {target_year}` to sign in."
        )
    effective_lang = _lang_override if _lang_override is not None else cfg.lang
    api_lang = None if effective_lang == "auto" else effective_lang
    return Site(
        url=f"{cfg.base_url}/{target_year}",
        token=cfg.tokens[target_year],
        year=target_year,
        lang=api_lang,
    )


def configured_years() -> list[int]:
    return sorted(load_config().tokens)
