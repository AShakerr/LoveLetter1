import base64
import datetime as dt
import html

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from desk.db import session_scope
from desk.decisions import run_pipeline
from desk.fixtures import load_fixtures
from desk.ingest.revolut import confirm_batch
from desk.models import Decision, Instrument
from desk.seed import load_all_seeds
from desk.universe import sync_instruments
from desk.web.app import create_app


def _auth():
    return {"Authorization": "Basic " + base64.b64encode(b"u:p").decode()}


@pytest.fixture
def client(settings):
    load_fixtures(settings)
    with session_scope(settings) as s:
        sync_instruments(s)
        load_all_seeds(s, settings)
        confirm_batch(s, "seed:positions_2026-09-04")
        run_pipeline(s, settings, dt.date(2026, 9, 4))
    with TestClient(create_app(settings)) as c:
        yield c


def test_decisions_list_and_detail(client, settings):
    r = client.get("/decisions", headers=_auth())
    assert r.status_code == 200
    body = html.unescape(r.text)
    assert "Regime 2026-09-04" in body and "COMMODITIES_POT" in body and "Paper vs actual" in body
    assert "regime_fit.yaml is missing" not in body
    assert "trigger" in body  # kill-condition trigger count on the held positions
    with session_scope(settings) as s:
        d = s.exec(select(Decision)).first()
        did = d.id
    r = client.get(f"/decisions/{did}", headers=_auth())
    assert (
        r.status_code == 200
        and "What would reverse this" in html.unescape(r.text)
        and "<table>" in r.text
    )
    r = client.post(
        f"/decisions/{did}/respond",
        data={"status": "skipped", "note": "not today"},
        headers=_auth(),
        follow_redirects=False,
    )
    assert r.status_code == 303
    body = html.unescape(client.get(f"/decisions/{did}", headers=_auth()).text)
    assert "skipped" in body and "not today" in body


def test_confirm_composition_route(client, settings):
    r = client.post(
        "/instruments/confirm-composition",
        data={"ticker": "COMMODITIES_POT"},
        headers=_auth(),
        follow_redirects=False,
    )
    assert r.status_code == 303
    with session_scope(settings) as s:
        assert (
            s.exec(select(Instrument).where(Instrument.ticker == "COMMODITIES_POT"))
            .one()
            .composition_confirmed
            is True
        )
    r = client.post("/decisions/run", headers=_auth(), follow_redirects=False)
    assert r.status_code == 303
    body = html.unescape(client.get("/decisions", headers=_auth()).text)
    assert "TRIM" in body
