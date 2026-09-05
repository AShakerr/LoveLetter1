import base64
import html

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from desk.db import session_scope
from desk.fixtures import load_fixtures
from desk.houseviews import change_log, current_stance, latest_views
from desk.ingest.revolut import confirm_batch
from desk.models import HouseView, Position, Regime, Report
from desk.portfolio import build_portfolio
from desk.seed import load_all_seeds
from desk.universe import sync_instruments
from desk.web.app import create_app
from tests.conftest import load_fixture


@pytest.fixture
def seeded(settings):
    load_fixtures(settings)
    with session_scope(settings) as s:
        sync_instruments(s)
        result = load_all_seeds(s, settings)
    return result


def test_seed_loads_everything(settings, seeded):
    assert len(seeded["house_views"]) == 6 and all(
        r["status"] == "ok" for r in seeded["house_views"]
    )
    assert seeded["positions"]["status"] == "pending" and seeded["positions"]["positions"] == 11
    assert seeded["regime"]["status"] == "ok"
    with session_scope(settings) as s:
        assert len(s.exec(select(Report)).all()) == 6
        assert len(s.exec(select(HouseView)).all()) == 89
        assert s.exec(select(Regime)).one().oil_state == "shock"
        # explicit changed_from from the reports survives
        ea = [
            v
            for v in s.exec(select(HouseView).where(HouseView.key == "Euro area")).all()
            if v.changed_from
        ]
        assert any(v.changed_from == "neutral" and v.stance == "most_preferred" for v in ea)
        # tactical Energy overweight must not become the house view
        assert current_stance(s, "sector", "Energy").view.stance == "least_preferred"
        views = latest_views(s)
        assert "sector" in views and "index_target" in views
        log = change_log(s)
        assert any(r.view.key == "Healthcare" and r.direction == "downgrade" for r in log)
        assert any(r.view.key == "S&P 500 Dec-26" and r.direction == "upgrade" for r in log)
        # idempotent
        again = load_all_seeds(s, settings)
        assert all(r["status"] == "already loaded" for r in again["house_views"])
        assert again["positions"]["status"] == "already loaded"
        # a position removed from the batch is re-added as pending when the seed file still lists it
        from desk.models import Instrument as _I

        ora = s.exec(select(_I).where(_I.ticker == "ORA")).one()
        for row in s.exec(select(Position).where(Position.instrument_id == ora.id)).all():
            s.delete(row)
        s.commit()
        ext = load_all_seeds(s, settings)["positions"]
        assert ext["status"] == "extended" and ext["positions"] == 1


def test_portfolio_valuation_and_limits(settings, seeded):
    with session_scope(settings) as s:
        view = build_portfolio(s, settings)
        assert view.basis.startswith("pending:seed:")
        assert view.total_eur > 0 and "USD" in view.fx and view.fx["USD"].per_eur
        by_ticker = {p.instrument.ticker: p for p in view.positions}
        assert by_ticker["CASH_USD"].value_native == pytest.approx(4240.79)
        assert by_ticker["COMMODITIES_POT"].value_native == pytest.approx(49676.18)
        # TSLA is priced from the (fixture) prices table, not from the snapshot
        assert by_ticker["TSLA"].price_source == "fixture:yfinance"
        # EUR conversion: USD value / (USD per EUR)
        usd = view.fx["USD"].per_eur
        assert by_ticker["CASH_USD"].value_eur == pytest.approx(4240.79 / usd)
        assert abs(sum(p.weight for p in view.positions) - 1) < 1e-9
        bars = {b.label: b for b in view.limits}
        assert by_ticker["WHOOP"].value_native == pytest.approx(25735.0)
        ora_close = load_fixture("yfinance.json")["ORA"][-1]["close"]
        assert (
            by_ticker["ORA"].value_native == pytest.approx(342 * ora_close)
            and by_ticker["ORA"].position.avg_cost == 14.01
        )
        assert by_ticker["WHOOP"].stale_after_days == 90 and not by_ticker["WHOOP"].is_stale
        assert (
            bars["Illiquid / private"].status == "breach"
            and "WHOOP" in bars["Illiquid / private"].detail
        )
        assert bars["Largest theme"].status == "breach" and bars["Largest theme"].detail == "gold"
        assert bars["Diversified core"].value == pytest.approx(
            sum(p.weight for p in view.positions if p.theme in ("us_broad", "em_broad"))
        )
        assert bars["Diversified core"].status == "breach"
        assert bars["Crypto"].value == 0
        assert any("unidentified" in w for w in view.warnings)
        # confirm makes it the live basis
        confirm_batch(s, view.basis[len("pending:") :])
        assert build_portfolio(s, settings).basis == "confirmed"


def _auth():
    return {"Authorization": "Basic " + base64.b64encode(b"u:p").decode()}


@pytest.fixture
def client(settings, seeded):
    with TestClient(create_app(settings)) as c:
        yield c


def test_portfolio_page(client):
    r = client.get("/portfolio", headers=_auth())
    assert r.status_code == 200
    body = html.unescape(r.text)
    assert "Unconfirmed snapshot" in body and "COMMODITIES_POT" in body and "Largest theme" in body
    assert "Pending snapshots" in body and "Confirm" in body
    r = client.post(
        "/positions/confirm",
        data={"batch": "seed:positions_2026-09-04"},
        headers=_auth(),
        follow_redirects=False,
    )
    assert r.status_code == 303
    body = html.unescape(client.get("/portfolio", headers=_auth()).text)
    assert "Confirmed positions" in body and "Pending snapshots" not in body


def test_house_views_page(client):
    r = client.get("/house-views", headers=_auth())
    assert r.status_code == 200
    body = html.unescape(r.text)
    for needle in (
        "Upgrade / downgrade log",
        "Healthcare",
        "most preferred",
        "Named risks",
        "S&P 500 Dec-26",
        "CrossAssetWeekly_20260828_en.pdf",
        "2 inconsistencies",
    ):
        assert needle in body, needle


def test_inbox_process_without_key(client, settings):
    (settings.reports_inbox / "x.pdf").write_bytes(b"%PDF")
    r = client.post("/inbox/process", headers=_auth(), follow_redirects=False)
    assert r.status_code == 303 and "ANTHROPIC_API_KEY" in r.headers["location"]
