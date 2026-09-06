import base64
import html
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from desk.db import session_scope
from desk.fixtures import load_fixtures
from desk.scheduler import build_scheduler
from desk.sources.base import Observation
from desk.tape import TAPE, TapeItem, TapeSpec, load_tape
from desk.web.app import create_app


def test_tape_from_fixtures(settings):
    load_fixtures(settings)
    with session_scope(settings) as s:
        items = {i.spec.label: i for i in load_tape(s, TAPE)}
    assert set(items) == {
        "S&P 500",
        "Brent",
        "Gold",
        "US 10y",
        "VIX",
        "Fed funds",
        "EZ HICP y/y",
        "Bitcoin",
    }
    assert all(i.value is not None for i in items.values()), {k: v.value for k, v in items.items()}
    assert items["Brent"].source == "fixture:yfinance" and items["US 10y"].source == "fixture:fred"
    assert items["S&P 500"].change is not None


def test_freshness_rules():
    spec = TapeSpec("x", "observation", "X")
    today = date.today()
    assert TapeItem(spec).freshness == "missing"
    assert (
        TapeItem(spec, value=1, as_of=today - timedelta(days=2), source="fred").freshness == "fresh"
    )
    assert (
        TapeItem(spec, value=1, as_of=today - timedelta(days=5), source="fred").freshness == "aging"
    )
    assert (
        TapeItem(spec, value=1, as_of=today - timedelta(days=9), source="fred").freshness == "stale"
    )
    assert (
        TapeItem(spec, value=1, as_of=today - timedelta(days=13), source="manual").freshness
        == "fresh"
    )
    assert (
        TapeItem(spec, value=1, as_of=today - timedelta(days=15), source="manual").freshness
        == "stale"
    )
    assert (
        TapeItem(spec, value=1, as_of=today - timedelta(days=15), source="fixture:manual").freshness
        == "stale"
    )
    monthly = TapeSpec("m", "observation", "M", frequency="monthly")
    assert (
        TapeItem(monthly, value=1, as_of=today - timedelta(days=40), source="ecb").freshness
        == "fresh"
    )
    assert (
        TapeItem(monthly, value=1, as_of=today - timedelta(days=60), source="ecb").freshness
        == "aging"
    )
    assert (
        TapeItem(monthly, value=1, as_of=today - timedelta(days=90), source="ecb").freshness
        == "stale"
    )


def _auth(user="u", pwd="p"):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pwd}".encode()).decode()}


@pytest.fixture
def client(settings):
    load_fixtures(settings)
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def test_basic_auth_required(client):
    assert client.get("/").status_code == 401
    assert client.get("/", headers=_auth("u", "wrong")).status_code == 401
    assert client.get("/healthz").status_code == 200


def test_dashboard_renders_tape(client):
    r = client.get("/", headers=_auth())
    assert r.status_code == 200
    body = html.unescape(r.text)
    for label in ("S&P 500", "Brent", "Gold", "US 10y", "VIX", "Fed funds", "EZ HICP", "Bitcoin"):
        assert label in body
    assert "fixture:yfinance" in body and "Run now" in body
    assert "fresh-stale" in body  # the placeholder manual series is > 14 days old


def test_run_now_uses_job(client, settings, monkeypatch):
    from desk.sources.base import Fetcher

    monkeypatch.setattr(
        settings, "offline", False
    )  # the stub never touches the network; let it report ok

    class Stub(Fetcher):
        name = "stub"

        def _raw(self):
            return {}

        def parse(self, raw):
            return [Observation(series="DGS10", date=date.today(), value=4.5, source="stub")]

    monkeypatch.setattr(
        "desk.jobs.build_fetchers", lambda universe, settings, *a, **k: [Stub(settings)]
    )
    r = client.post("/jobs/run", headers=_auth())
    assert r.status_code == 200 and "Run finished: 1 ok" in r.text
    tape = client.get("/api/tape", headers=_auth()).json()
    ten = next(t for t in tape if t["key"] == "DGS10")
    assert ten["value"] == 4.5 and ten["source"] == "stub" and ten["freshness"] == "fresh"


def test_scheduler_jobs(settings):
    sched = build_scheduler(settings)
    jobs = {j.id: j for j in sched.get_jobs()}
    assert {
        "daily_fetch",
        "nightly_backup",
        "inbox_scan",
        "fundamentals_weekly",
        "screener_refresh",
        "weekly_digest",
    } <= set(jobs)
    trig = jobs["daily_fetch"].trigger
    assert str(trig.timezone) == "Europe/Berlin"
    assert str(trig.fields[trig.FIELD_NAMES.index("hour")]) == "7"
