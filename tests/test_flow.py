"""Brief 8d, steps 1-3: Form 4 fetcher and parser, classification, the routine-trade rule, signals, Flow page."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from fastapi.testclient import TestClient
from sqlmodel import select

from desk.db import session_scope
from desk.flow import (
    active_signals,
    compute_signals,
    flow_badge,
    flow_tickers,
    is_routine_trade,
    page_data,
    run_flow_daily,
    store_trades,
)
from desk.models import DisclosedTrade, FlowSignal, Instrument
from desk.sources.form4 import (
    Form4Fetcher,
    business_days,
    classify,
    extract_ownership_xml,
    parse_form_index,
    parse_ownership_xml,
    trades_from_raw,
)
from desk.universe import sync_instruments
from tests.conftest import load_fixture

TODAY = dt.date(2026, 9, 6)
SAMPLE = Path(__file__).parent / "fixtures" / "form4_sample.json"


def _sample():
    return json.loads(SAMPLE.read_text(encoding="utf-8"))


def test_form_index_and_xml_parsing():
    idx = (
        "Description: Daily Index of EDGAR Dissemination Feed by Form Type\n"
        "Last Data Received: September 4, 2026\n\n"
        "Form Type   Company Name                CIK         Date Filed  File Name\n"
        "---------------------------------------------------------------------------\n"
        "10-K        ACME CORP                   12345       20260904    edgar/data/12345/0000012345-26-000001.txt\n"
        "4           STANLEY BLACK & DECKER, INC. 93556      20260904    edgar/data/93556/0000093556-26-000101.txt\n"
        "4/A         NVIDIA CORP                 1045810     20260904    edgar/data/1045810/0001045810-26-000999.txt\n"
    )
    rows = parse_form_index(idx)
    assert [r["form"] for r in rows] == ["10-K", "4", "4/A"]
    assert rows[1]["cik"] == "93556" and rows[1]["company"] == "STANLEY BLACK & DECKER, INC."
    assert rows[1]["file"].endswith("0000093556-26-000101.txt") and rows[1]["filed"] == "20260904"
    sample = _sample()
    doc = parse_ownership_xml(sample["filings"][3]["xml"])  # NVDA 10b5-1 sale via footnote
    assert doc["issuer_ticker"] == "NVDA" and doc["owners"][0]["name"] == "Huang Jen Hsun"
    assert doc["owners"][0]["role"] == "director, President and CEO"
    tx = doc["transactions"][0]
    assert tx["code"] == "S" and tx["shares"] == 75000 and tx["price"] == 231.4 and tx["is_10b5_1"]
    doc2 = parse_ownership_xml(sample["filings"][4]["xml"])  # document-level aff10b5One flag
    assert all(t["is_10b5_1"] for t in doc2["transactions"])
    wrapped = (
        "<SEC-DOCUMENT>...\n<DOCUMENT>\n<TYPE>4\n<XML>\n"
        + sample["filings"][0]["xml"]
        + "\n</XML>\n</DOCUMENT>"
    )
    assert extract_ownership_xml(wrapped).startswith("<?xml")
    assert extract_ownership_xml("<SEC-DOCUMENT>nothing</SEC-DOCUMENT>") is None


def test_classification_rules():
    assert classify({"code": "P", "asset_type": "stock"}) == {
        "side": "buy",
        "asset_type": "stock",
        "is_open_market": True,
    }
    assert classify({"code": "S", "asset_type": "stock"})["side"] == "sell"
    for code in ("A", "M", "F", "G", "J"):
        c = classify({"code": code, "asset_type": "stock", "acquired_disposed": "A"})
        assert c["asset_type"] == "other" and not c["is_open_market"]
    assert (
        classify({"code": "P", "asset_type": "option"})["asset_type"] == "other"
    )  # derivative rows score zero
    rows = trades_from_raw(_sample())
    assert len(rows) == 8
    swk = [r for r in rows if r["issuer_ticker"] == "SWK"]
    assert len(swk) == 3 and all(
        r["is_open_market"] and r["side"] == "buy" and not r["is_10b5_1"] for r in swk
    )
    assert {r["lag_days"] for r in swk} == {2}
    nv = [r for r in rows if r["issuer_ticker"] == "NVDA"]
    assert all(r["is_10b5_1"] for r in nv)
    tsla = next(r for r in rows if r["issuer_ticker"] == "TSLA")
    assert (
        tsla["asset_type"] == "other"
        and tsla["transaction_code"] == "A"
        and tsla["code_name"] == "grant/award"
    )


def test_fetcher_filters_to_the_universe_and_respects_edgar_rules(settings, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "edgar_user_agent", None)
    assert Form4Fetcher(["SWK"], settings=settings).enabled()[0] is False
    monkeypatch.setattr(settings, "edgar_user_agent", "desk ahmed@example.com")
    assert Form4Fetcher([], settings=settings).enabled()[0] is False
    f = Form4Fetcher(
        ["SWK", "MU", "NOPE"],
        settings=settings,
        days=2,
        today=dt.date(2026, 9, 7),
        sleep=lambda s: None,
    )
    sample = _sample()
    idx = (
        "header\n---\n"
        "4  STANLEY BLACK & DECKER, INC. 93556  20260904  edgar/data/93556/0000093556-26-000101.txt\n"
        "4  NVIDIA CORP 1045810 20260904 edgar/data/1045810/0001045810-26-000201.txt\n"
        "8-K STANLEY BLACK & DECKER, INC. 93556 20260904 edgar/data/93556/0000093556-26-000777.txt\n"
    )
    calls: list[str] = []

    def fake_get(url: str) -> str:
        calls.append(url)
        if url.endswith("company_tickers.json"):
            return json.dumps(
                {
                    "0": {"cik_str": 93556, "ticker": "SWK"},
                    "1": {"cik_str": 723125, "ticker": "MU"},
                    "2": {"cik_str": 1045810, "ticker": "NVDA"},
                }
            )
        if "form.20260904.idx" in url:
            return idx
        if "form.20260907.idx" in url or "form.20260903.idx" in url:
            raise RuntimeError("404 not yet published")
        if url.endswith("0000093556-26-000101.txt"):
            return "<SEC-DOCUMENT>\n<XML>\n" + sample["filings"][0]["xml"] + "\n</XML>"
        raise AssertionError(f"unexpected fetch {url}")

    monkeypatch.setattr(f, "_get", fake_get)
    raw = f._raw()
    assert business_days(dt.date(2026, 9, 7), 3) == [
        dt.date(2026, 9, 3),
        dt.date(2026, 9, 4),
        dt.date(2026, 9, 7),
    ]
    assert raw["tickers"] == {"SWK": "93556", "MU": "723125"}  # NOPE has no CIK
    assert [x["ticker"] for x in raw["filings"]] == [
        "SWK"
    ]  # NVDA is not in this fetcher's universe; 8-K ignored
    assert (
        raw["filings"][0]["url"].endswith("0000093556-26-000101.txt")
        and raw["filings"][0]["filed"] == "2026-09-04"
    )
    assert any(
        "2026-09-07" in e for e in raw["_errors"]
    )  # the unpublished day is recorded, not fatal
    assert (settings.cache_dir / "edgar_company_tickers.json").exists()  # cached CIK map
    assert f.parse(raw) == [] and len(f.trades(raw)) == 1


def test_store_signals_badges_and_routine_rule(settings):
    from desk.screener import refresh_constituents

    with session_scope(settings) as s:
        sync_instruments(s)
        doc = load_fixture("constituents.json")
        refresh_constituents(s, settings, fetch=lambda src: doc.get(src, []))
        rows = trades_from_raw(_sample())
        counts = store_trades(s, rows)
        assert counts == {"inserted": 8, "duplicate": 0, "unknown_issuer": 0}
        assert store_trades(s, rows)["duplicate"] == 8  # idempotent
        sigs = compute_signals(s, TODAY)
        by = {(s_.instrument_id, s_.signal) for s_ in sigs}
        swk = s.exec(select(Instrument).where(Instrument.ticker == "SWK")).one()
        nvda = s.exec(select(Instrument).where(Instrument.ticker == "NVDA")).one()
        mu = s.exec(select(Instrument).where(Instrument.ticker == "MU")).one()
        assert (swk.id, "insider_cluster_buy") in by
        assert (nvda.id, "insider_cluster_buy") not in by and (
            nvda.id,
            "insider_sale_cluster",
        ) not in by  # 10b5-1 sales score zero
        assert (mu.id, "insider_cluster_buy") not in by  # one buyer is not a cluster
        cluster = next(
            x for x in sigs if x.instrument_id == swk.id and x.signal == "insider_cluster_buy"
        )
        assert cluster.detail_json["n"] == 3 and cluster.scored and cluster.strength > 0
        badge = flow_badge(s, swk.id, TODAY)
        assert (
            badge.startswith("insider cluster buy: 3 filers")
            and "Allan Donald" in badge
            and "last 2026-09-03" in badge
        )
        assert flow_badge(s, nvda.id, TODAY) is None
        net = next(x for x in sigs if x.instrument_id == swk.id and x.signal == "insider_net_flow")
        assert (
            net.strength is None
            and "insufficient history" in net.detail_json["note"]
            and not net.scored
        )
        # routine rule: same filer, same issuer, same calendar month in 2 of the prior 3 years
        for y in (2024, 2025):
            s.add(
                DisclosedTrade(
                    source="form4",
                    filer_name="Allan Donald",
                    issuer_ticker="SWK",
                    instrument_id=swk.id,
                    trade_date=dt.date(y, 9, 2),
                    filed_date=dt.date(y, 9, 4),
                    lag_days=2,
                    side="buy",
                    transaction_code="P",
                    quantity=1000,
                    price=80.0,
                    is_open_market=True,
                    raw_url=f"https://example/{y}",
                    fetched_at=dt.datetime(2026, 9, 6),
                )
            )
        s.commit()
        assert is_routine_trade(s, "Allan Donald", "SWK", dt.date(2026, 9, 2)) is True
        assert is_routine_trade(s, "Allan Donald", "SWK", dt.date(2026, 10, 2)) is False
        assert is_routine_trade(s, "Raff Patrick", "SWK", dt.date(2026, 9, 2)) is False
        # re-storing the same 2026 rows is a duplicate; a fresh routine filing would be flagged at insert
        new = dict(
            rows[0],
            raw_url="https://example/new",
            trade_date=dt.date(2026, 9, 5),
            filed_date=dt.date(2026, 9, 7),
        )
        store_trades(s, [new])
        stored = s.exec(
            select(DisclosedTrade).where(DisclosedTrade.raw_url == "https://example/new")
        ).one()
        assert stored.is_routine is True
        sigs = compute_signals(s, TODAY)
        assert (swk.id, "insider_cluster_buy") in {
            (x.instrument_id, x.signal) for x in sigs
        }  # the other two buys still count; Allan's original 2026-09-02 row keeps its flag
        assert len(active_signals(s, swk.id, TODAY)) >= 1
        tickers = flow_tickers(s)
        assert "SWK" in tickers and "TSLA" not in tickers  # TSLA is not held in this bare DB
        page = page_data(s, TODAY, days=3)
        assert page["counts"]["filings"] == 8 and page["counts"]["open_market_buys"] == 4
        assert page["watch"] == []
        why = {r["t"].issuer_ticker + r["t"].transaction_code: r["why_zero"] for r in page["rows"]}
        assert why["NVDAS"] == "10b5-1 plan" and why["TSLAA"].startswith("not open-market")
        assert (
            page_data(s, TODAY, signal="insider_cluster_buy")["counts"]["trades"] == 4
        )  # SWK rows only (3 + the routine one)


def test_flow_job_and_page(settings, monkeypatch):
    from desk.web.app import create_app

    class Stub(Form4Fetcher):
        def _raw(self):
            return _sample()

    monkeypatch.setattr(settings, "edgar_user_agent", "desk ahmed@example.com")
    monkeypatch.setattr(settings, "offline", False)
    with session_scope(settings) as s:
        sync_instruments(s)
        from desk.screener import refresh_constituents

        doc = load_fixture("constituents.json")
        refresh_constituents(s, settings, fetch=lambda src: doc.get(src, []))
    res = run_flow_daily(settings, TODAY, fetcher=Stub(["SWK"], settings=settings, today=TODAY))
    assert res["status"] == "ok" and res["rows"] == 8 and res["signals"] >= 1
    client = TestClient(create_app(settings))
    import base64

    auth = {"Authorization": "Basic " + base64.b64encode(b"u:p").decode()}
    r = client.get("/flow", headers=auth)
    assert r.status_code == 200
    body = r.text
    assert "insider cluster buy" in body and "Allan Donald" in body and "10b5-1" in body
    assert "EDGAR" in body and "0000093556-26-000101" in body
    assert "Watch list" in body and "Empty" in body
    r2 = client.get("/flow?source=house", headers=auth)
    assert (
        r2.status_code == 200 and "0000093556-26-000101" not in r2.text
    )  # filings filtered; signals stay
    with session_scope(settings) as s:
        assert s.exec(select(FlowSignal)).all()


def test_weekly_fundamentals_skips_european_names_without_a_venue_symbol(settings):
    from desk.fundamentals import run_weekly
    from desk.screener import refresh_constituents

    calls: list[str] = []

    def fake_fetch(symbol):
        calls.append(symbol)
        return {"trailingPE": 10.0}

    with session_scope(settings) as s:
        sync_instruments(s)
        refresh_constituents(
            s,
            settings,
            fetch=lambda src: {
                "sp500": [
                    {
                        "ticker": "AAPL",
                        "name": "Apple",
                        "sector": "Information Technology",
                        "exchange": "NASDAQ",
                        "region": "USA",
                        "currency": "USD",
                        "source_symbol": "AAPL",
                    }
                ],
                "stoxx600": [
                    {
                        "ticker": "ZURN",
                        "name": "Zurich",
                        "sector": "Insurance",
                        "exchange": "Europe",
                        "region": "Euro area",
                        "currency": "EUR",
                        "source_symbol": None,
                    },
                    {
                        "ticker": "SAP",
                        "name": "SAP SE",
                        "sector": "Technology",
                        "exchange": "Xetra",
                        "region": "Euro area",
                        "currency": "EUR",
                        "source_symbol": "SAP.DE",
                    },
                ],
            }.get(src, []),
        )
        members = [
            i for i in s.exec(select(Instrument)).all() if i.ticker in ("AAPL", "ZURN", "SAP")
        ]
        res = run_weekly(
            s,
            settings,
            instruments=members,
            fetch=fake_fetch,
            fetch_earnings=None,
            av_fallback=True,
        )
        assert "ZURN" in res["skipped_no_venue_symbol"] and "ZURN" not in calls
        assert set(calls) == {"AAPL", "SAP.DE"} and res["ok"] == 2
