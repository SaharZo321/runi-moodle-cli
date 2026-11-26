"""Shared fixtures for offline Moodle CLI tests."""

from __future__ import annotations

import pytest
import respx

from runi_moodle_cli import client
from runi_moodle_cli.config import Config, save_config


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    data_dir = tmp_path / "data"
    monkeypatch.setenv("RUNI_MOODLE_CONFIG", str(cfg_file))
    monkeypatch.setenv("RUNI_MOODLE_DATA_DIR", str(data_dir))
    for key in list(monkeypatch._setitem) if hasattr(monkeypatch, "_setitem") else []:
        pass
    monkeypatch.delenv("MOODLE_BASE_URL", raising=False)
    monkeypatch.delenv("MOODLE_DEFAULT_YEAR", raising=False)
    monkeypatch.delenv("MOODLE_TOKEN_2027", raising=False)
    monkeypatch.delenv("MOODLE_TOKEN_2026", raising=False)
    client._user_id_cache.clear()
    yield cfg_file


@pytest.fixture
def configured_site(isolated_config):
    save_config(
        Config(
            base_url="https://moodle.runi.ac.il",
            default_year=2027,
            tokens={2027: "tok2027secretvalue", 2026: "tok2026secretvalue"},
        )
    )
    return isolated_config


@pytest.fixture
def moodle_api():
    with respx.mock(assert_all_called=False) as mock:
        yield mock
