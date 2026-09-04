from datetime import date

from sqlmodel import select

from desk.db import session_scope
from desk.fixtures import load_fixtures
from desk.jobs import backup_sqlite, run_daily
from desk.models import FetchRun, NewsSentiment, Price
from desk.models import Observation as ObsRow
from desk.persist import persist_observations
from desk.sources.base import Fetcher, Observation
from desk.universe import sync_instruments


def _obs_set():
    return [
        Observation.price(
            "^GSPC", date(2026, 9, 3), 7450.0, source="t", open=7400, high=7460, low=7390, volume=1
        ),
        Observation(series="DGS10", date=date(2026, 9, 3), value=4.51, source="t"),
        Observation.news(date(2026, 9, 3), 0.12, source="t", ticker="TSLA", volume=3),
        Observation.news(date(2026, 9, 3), -1.2, source="t", topic="ECB", volume=40),
        Observation.price("NOPE", date(2026, 9, 3), 1.0, source="t"),
    ]


def test_persist_routes_and_is_idempotent(settings):
    with session_scope(settings) as s:
        sync_instruments(s)
        c1 = persist_observations(s, _obs_set())
        assert c1["prices"] == 1 and c1["observations"] == 1 and c1["news"] == 2
        assert c1["skipped_unknown_ticker"] == 1
        c2 = persist_observations(s, _obs_set())
        assert sum(v for k, v in c2.items() if not k.startswith("skipped")) == 0
        assert len(s.exec(select(Price)).all()) == 1
        assert len(s.exec(select(NewsSentiment)).all()) == 2
        updated = _obs_set()
        updated[1].value = 4.60
        c3 = persist_observations(s, updated)
        assert c3["observations_updated"] == 1
        row = s.exec(select(ObsRow).where(ObsRow.series == "DGS10")).one()
        assert row.value == 4.60


class Stub(Fetcher):
    name = "stub"

    def _raw(self):
        return {"ok": True}

    def parse(self, raw):
        return [Observation(series="DFF", date=date(2026, 9, 3), value=4.33, source="stub")]


def test_run_daily_records_fetch_runs(settings):
    summary = run_daily(settings, fetchers=[Stub(settings)], decide=False)
    assert summary == [
        {
            "source": "stub",
            "status": "ok",
            "rows": 1,
            "observations": 1,
            "error": None,
            "counts": {"observations": 1},
        }
    ]
    with session_scope(settings) as s:
        runs = s.exec(select(FetchRun)).all()
        assert len(runs) == 1 and runs[0].status == "ok" and runs[0].rows == 1


def test_load_fixtures_tags_source(settings):
    summary = load_fixtures(settings)
    assert all(s["status"] == "ok" for s in summary), summary
    with session_scope(settings) as s:
        assert {p.source for p in s.exec(select(Price)).all()} == {"fixture:yfinance"}
        assert s.exec(select(ObsRow).where(ObsRow.series == "DGS10")).first() is not None


def test_backup_keeps_n(settings, monkeypatch):
    load_fixtures(settings)
    monkeypatch.setattr(settings, "backups_to_keep", 2)
    paths = [backup_sqlite(settings) for _ in range(3)]
    assert all(p.stat().st_size > 0 for p in paths[-2:])
    assert len(list(settings.backup_dir.glob("desk_*.sqlite3"))) == 2
