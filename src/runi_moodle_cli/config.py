"""Cross-platform configuration and token storage (Windows, macOS, Linux).

Loads lazily from the user's config file (via platformdirs) with optional
overrides from environment variables / .env files.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
from platformdirs import user_config_dir, user_data_dir

APP_NAME = "runi-moodle-cli"
DEFAULT_BASE_URL = "https://moodle.runi.ac.il"
DEFAULT_LANG = "en"
VALID_LANGS = frozenset({"en", "he", "auto"})

_lang_override: str | None = None


class AuthError(RuntimeError):
    """Raised when Moodle authentication is missing or invalid."""


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
    raw = json.loads(path.read_text(encoding="utf-8"))
    tokens: dict[int, str] = {}
    for k, v in (raw.get("tokens") or {}).items():
        if v:
            tokens[int(k)] = str(v)
    default_year = raw.get("default_year")
    raw_lang = raw.get("lang")
    lang = normalize_lang(str(raw_lang)) if raw_lang else DEFAULT_LANG
    return Config(
        base_url=str(raw.get("base_url") or DEFAULT_BASE_URL).rstrip("/"),
        default_year=int(default_year) if default_year is not None else None,
        tokens=tokens,
        lang=lang,
    )


def load_config() -> Config:
    """Load config from disk, then apply .env / environment variable overrides."""
    load_dotenv()
    cfg = _load_file_config(config_path())

    if env_base := os.environ.get("MOODLE_BASE_URL"):
        cfg.base_url = env_base.rstrip("/")

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


def save_config(cfg: Config) -> Path:
    """Save config to disk with restrictive permissions on POSIX systems."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "base_url": cfg.base_url.rstrip("/"),
        "default_year": cfg.default_year,
        "lang": cfg.lang,
        "tokens": {str(year): token for year, token in sorted(cfg.tokens.items())},
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    if os.name != "nt":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return path


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
        cfg.base_url = base_url.rstrip("/")
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
