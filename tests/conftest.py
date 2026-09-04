"""Every test runs with the network off (pytest-socket) against a throwaway data dir."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from desk import config as cfg
from desk import db as dbmod

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> cfg.Settings:
    monkeypatch.setenv("DESK_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DESK_SCHEDULER_ENABLED", "0")
    monkeypatch.setenv("DESK_BASIC_AUTH_USER", "u")
    monkeypatch.setenv("DESK_BASIC_AUTH_PASS", "p")
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    monkeypatch.delenv("ALPHAVANTAGE_API_KEY", raising=False)
    cfg.reset_settings_cache()
    dbmod.reset_engine()
    s = cfg.Settings(_env_file=None)
    s.ensure_dirs()
    dbmod.init_db(s)
    yield s
    dbmod.reset_engine()
    cfg.reset_settings_cache()
