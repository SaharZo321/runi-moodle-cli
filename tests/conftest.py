"""Shared fixtures for offline Moodle CLI tests."""

from __future__ import annotations

import os

import pytest
import respx

from runi_moodle_cli import client
from runi_moodle_cli.config import Config, save_config, set_lang_override


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    cfg_file = tmp_path / "config.json"
    data_dir = tmp_path / "data"
    monkeypatch.setenv("RUNI_MOODLE_CONFIG", str(cfg_file))
    monkeypatch.setenv("RUNI_MOODLE_DATA_DIR", str(data_dir))
    for key in list(os.environ):
        if key.startswith("MOODLE_"):
            monkeypatch.delenv(key, raising=False)
    set_lang_override(None)
    client._user_id_cache.clear()
    yield cfg_file
    set_lang_override(None)


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
