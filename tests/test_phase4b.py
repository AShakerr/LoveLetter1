"""Phase 4 second half: self-scoring, promotion checklist, screener track record, seeded ideas, digest, narrative,
earnings dates, fixture loaders, and the 7b sentiment adjuster."""

import datetime as dt
import json

import pytest
from sqlmodel import select

from desk.crowd import crowd_factor, sentiment_adjust
from desk.db import session_scope
from desk.digest import build_digest, send_digest
from desk.fixtures import load_fixtures
from desk.fundamentals import run_weekly
from desk.ingest.revolut import confirm_batch
from desk.models import (
    Decision,
    Event,
    Instrument,
    NewsSentiment,
    Price,
    RuleFired,
    Score,
    ScreenerRow,
)
from desk.narrative import add_narratives
from desk.seed import load_all_seeds
from desk.trackrecord import (
    attribution,
    blend_return,
    decision_outcomes,
    hit_rate,
    max_stale_streak,
    promotion_checklist,
    screener_track,
    seeded_ideas,
)
from desk.universe import sync_instruments

TODAY = dt.date(
    2026, 9, 3
)  # last fixture price date, so 30-day windows from early August have matured


@pytest.fixture
def seeded(settings):
    load_fixtures(settings)
    with session_scope(settings) as s:
        sync_instruments(s)
        load_all_seeds(s, settings)
        confirm_batch(s, "seed:positions_2026-09-04")
    return settings


def _inst(s, t):
    return s.exec(select(Instrument).where(Instrument.ticker == t)).one()


def _decision(s, inst, on, action, size=None, flags=None, score=None, status="pending"):
    d = Decision(
        date=on,
        instrument_id=inst.id,
        action=action,
        size_pct=size,
        score_id=score.id if score else None,
        rules_json={"flags": flags or [], "reference_price": None},
        reasoning_md=f"# {action}",
        created_at=dt.datetime.utcnow(),
        user_status=status,
    )
    s.add(d)
    s.commit()
    s.refresh(d)
    return d


def test_sentiment_adjuster(seeded):
    with session_scope(seeded) as s:
        tsla = _inst(s, "TSLA")
        adj, info = sentiment_adjust(s, tsla, TODAY)
        assert info["level"] == "ticker" and adj in (-1, 0, 1)
        ora = _inst(s, "ORA")  # no ticker sentiment: sector/topic fallback, flagged
        adj2, info2 = sentiment_adjust(s, ora, TODAY)
        assert info2["level"] == "sector" and info2["note"] == "sector-level sentiment only"
        # force a strongly positive ticker sentiment and a mid-range percentile: crowd = 3 + surprise + 1
        for i in range(5):
            s.add(
                NewsSentiment(
                    instrument_id=ora.id,
                    date=TODAY - dt.timedelta(days=i),
                    score=0.5,
                    volume=3,
                    source="test",
                    fetched_at=dt.datetime.utcnow(),
                )
            )
        s.commit()
        assert sentiment_adjust(s, ora, TODAY)[0] == 1
        res = crowd_factor(s, ora, TODAY)
        assert res.inputs["sentiment"]["adjust"] == 1
        if res.percentile is not None and 30 <= res.percentile <= 70:
            assert res.value >= 4


def test_outcomes_hit_rates_and_attribution(seeded):
    with session_scope(seeded) as s:
        vusa, tsla, x9 = _inst(s, "VUSA"), _inst(s, "TSLA"), _inst(s, "NVDA")
        early = dt.date(2026, 7, 20)
        sc = Score(
            instrument_id=vusa.id,
            date=early,
            total=80,
            f_safra=5,
            f_regime=3,
            f_portfolio=5,
            f_valuation=4,
            f_momentum=4,
            f_season=0,
            f_crowd=3,
            inputs_json={
                "factors": {
                    "safra": {"value": 5},
                    "regime": {"value": 3},
                    "portfolio": {"value": 5},
                    "valuation": {"value": 4},
                    "momentum": {"value": 4},
                    "crowd": {"value": 3},
                    "season": {"value": 0},
                }
            },
        )
        s.add(sc)
        s.commit()
        buy = _decision(s, vusa, early, "BUY", 0.05, score=sc, status="executed")
        sell = _decision(
            s,
            tsla,
            early,
            "SELL",
            0.03,
            flags=[{"rule": "stop_loss", "severity": "mandatory", "summary": "x"}],
        )
        hold = _decision(s, x9, early, "HOLD")
        recent = _decision(s, vusa, dt.date(2026, 8, 25), "ADD", 0.02)
        outs = decision_outcomes(s, TODAY, seeded, windows=(30, 90))
        by = {(o.decision.id, o.window): o for o in outs}
        b30 = by[(buy.id, 30)]
        assert (
            b30.matured and b30.instrument_return is not None and b30.benchmark_return is not None
        )
        ret, _ = __import__("desk.trackrecord", fromlist=["instrument_return"]).instrument_return(
            s, vusa.id, early, early + dt.timedelta(days=30)
        )
        assert b30.instrument_return == pytest.approx(ret)
        assert b30.cost_source == "costs.yaml estimate" and b30.cost_bps == 5  # ETF spread
        assert b30.net_excess == pytest.approx(ret - 5 / 1e4 - b30.benchmark_return)
        assert b30.hit == (b30.net_excess > 0) and b30.rule == "buy_side" and b30.factor == "safra"
        s30 = by[(sell.id, 30)]
        assert s30.rule == "stop_loss" and s30.net_excess == pytest.approx(
            -10 / 1e4 - s30.instrument_return
        )
        h30 = by[(hold.id, 30)]
        assert h30.net_excess == pytest.approx(h30.instrument_return)
        assert not by[(buy.id, 90)].matured and not by[(recent.id, 30)].matured
        hr = hit_rate(outs)
        assert hr[30]["n"] == 3 and 90 not in hr
        rules = {g["key"]: g for g in attribution(outs, "rule", 30)}
        assert set(rules) == {"buy_side", "stop_loss", "hold"} and rules["buy_side"]["n"] == 1
        factors = {g["key"]: g for g in attribution(outs, "factor", 30)}
        assert "safra" in factors and "n/a" in factors
        bench, used = blend_return(
            s, ("VUSA", "EXW1"), ("^GSPC", "^STOXX50E"), early, early + dt.timedelta(days=30)
        )
        assert bench is not None and used == ["VUSA", "EXW1"]


def test_promotion_checklist_and_stale_streak(seeded):
    with session_scope(seeded) as s:
        checklist = {c.name: c for c in promotion_checklist(s, TODAY, seeded)}
        assert len(checklist) == 5
        assert not checklist["At least 60 trading days of paper decisions"].passed
        assert checklist["No data-staleness incident longer than 3 days"].passed
        assert checklist["Every decision reviewed (no pending older than 7 days)"].passed
        for i in range(5):
            s.add(
                RuleFired(
                    date=dt.date(2026, 8, 1) + dt.timedelta(days=i),
                    rule="stale_data",
                    severity="review",
                    detail_json={},
                )
            )
        s.commit()
        assert max_stale_streak(s) == 5
        checklist = {c.name: c for c in promotion_checklist(s, TODAY, seeded)}
        assert not checklist["No data-staleness incident longer than 3 days"].passed
        _decision(s, _inst(s, "TSLA"), dt.date(2026, 8, 1), "HOLD")
        checklist = {c.name: c for c in promotion_checklist(s, TODAY, seeded)}
        assert not checklist["Every decision reviewed (no pending older than 7 days)"].passed


def test_screener_track_and_seeded_ideas(seeded):
    with session_scope(seeded) as s:
        nvda, tsla = _inst(s, "NVDA"), _inst(s, "TSLA")
        d0 = dt.date(2026, 7, 15)
        for rank, inst in enumerate((nvda, tsla), 1):
            s.add(
                ScreenerRow(
                    date=d0,
                    instrument_id=inst.id,
                    rank=rank,
                    total=80 - rank,
                    factors_json={},
                    gates_json={"passed": True},
                )
            )
        s.add(
            ScreenerRow(
                date=dt.date(2026, 8, 30),
                instrument_id=nvda.id,
                rank=1,
                total=80,
                factors_json={},
                gates_json={"passed": True},
            )
        )
        s.commit()
        st = screener_track(s, TODAY)
        rows = {r["date"]: r for r in st["rows"]}
        w30 = rows[d0]["windows"][30]
        assert (
            w30["matured"]
            and w30["priced"] == 2
            and w30["benchmark"] is not None
            and "^STOXX" in w30["bench_used"]
        )
        assert (
            not rows[d0]["windows"][90]["matured"]
            and not rows[dt.date(2026, 8, 30)]["windows"][30]["matured"]
        )
        assert st["summary"][30]["n"] == 1 and st["summary"][90]["n"] == 0
        ideas = seeded_ideas(s, TODAY)
        kinds = {(i["kind"], i["key"]) for i in ideas}
        assert (
            ("stock", "NVDA") in kinds
            and ("index_target", "S&P 500 Dec-26") in kinds
            and ("commodity", "Gold Dec-26") in kinds
        )
        nv = next(i for i in ideas if i["key"] == "NVDA")
        assert (
            nv["stance"] == "buy" and nv["return"] is not None and nv["hit"] == (nv["return"] > 0)
        )
        spx = next(i for i in ideas if i["key"] == "S&P 500 Dec-26")
        assert spx["target"] == 8200 and spx["progress"] is not None


def test_digest_builds_and_writes_file(seeded):
    with session_scope(seeded) as s:
        subject, body = build_digest(s, seeded, TODAY)
        assert (
            subject == "desk digest 2026-09-03" and "Promotion checklist" in body and "FAIL" in body
        )
        assert "Paper vs actual" in body
    result = send_digest(subject, body, seeded)
    assert "written to" in result and (seeded.data_dir / "digests" / "2026-09-03.md").exists()


def test_narrative_optional(seeded, monkeypatch):
    class Fake:
        def complete(self, system, content, *, max_tokens=16000):
            return "  A plain paragraph.  "

    with session_scope(seeded) as s:
        d = _decision(s, _inst(s, "VUSA"), TODAY, "HOLD")
        assert add_narratives(s, [d], seeded, Fake()) == 0  # disabled by default
        monkeypatch.setattr(seeded, "llm_reasoning", True)
        assert (
            add_narratives(s, [d], seeded, Fake()) == 1 and d.narrative_md == "A plain paragraph."
        )
        assert add_narratives(s, [d], seeded, Fake()) == 0  # never overwritten


def test_weekly_job_records_earnings_events(seeded):
    calls = []

    def fake_info(symbol):
        return {"trailingPE": 20.0, "forwardPE": 18.0, "_sector": None}

    def fake_earnings(symbol):
        calls.append(symbol)
        return [
            {"date": "2026-10-22", "eps_estimate": 0.9, "reported_eps": None},
            {"date": "2026-07-23", "eps_estimate": 0.8, "reported_eps": 0.95},
        ]

    with session_scope(seeded) as s:
        tsla = _inst(s, "TSLA")
        res = run_weekly(
            s,
            seeded,
            instruments=[tsla],
            fetch=fake_info,
            av_fallback=False,
            on=TODAY,
            fetch_earnings=fake_earnings,
        )
        assert res["ok"] == 1 and res["earnings_events"] == 2 and calls == ["TSLA"]
        evs = s.exec(
            select(Event)
            .where(Event.instrument_id == tsla.id, Event.kind == "earnings")
            .order_by(Event.date)
        ).all()
        assert [e.date.isoformat() for e in evs] == ["2026-07-23", "2026-10-22"] and evs[
            0
        ].surprise == pytest.approx(0.15)
        # re-running upserts rather than duplicating
        run_weekly(
            s,
            seeded,
            instruments=[tsla],
            fetch=fake_info,
            av_fallback=False,
            on=TODAY,
            fetch_earnings=fake_earnings,
        )
        assert (
            len(
                s.exec(
                    select(Event).where(Event.instrument_id == tsla.id, Event.kind == "earnings")
                ).all()
            )
            == 2
        )


def test_fixture_loaders_for_screener(settings, tmp_path):
    from desk.fixtures import load_fixture_constituents, load_fixture_screener_prices

    load_fixtures(settings)
    cons = tmp_path / "constituents.json"
    cons.write_text(
        json.dumps(
            {
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
                "stoxx600": [],
            }
        )
    )
    prices = tmp_path / "screener_prices.json"
    prices.write_text(
        json.dumps(
            {
                "AAPL": [
                    {
                        "date": "2026-09-01",
                        "open": 200,
                        "high": 201,
                        "low": 199,
                        "close": 200.5,
                        "volume": 1,
                    },
                    {
                        "date": "2026-09-02",
                        "open": 201,
                        "high": 202,
                        "low": 200,
                        "close": 201.5,
                        "volume": 1,
                    },
                ]
            }
        )
    )
    with session_scope(settings) as s:
        sync_instruments(s)
        assert load_fixture_constituents(s, settings, cons)["rows"] == 1
        assert load_fixture_screener_prices(s, settings, prices)["rows"] == 2
        aapl = _inst(s, "AAPL")
        assert (
            aapl.screener_member == "sp500"
            and len(s.exec(select(Price).where(Price.instrument_id == aapl.id)).all()) == 2
        )
        assert (
            load_fixture_constituents(s, settings, tmp_path / "missing.json")["status"] == "missing"
        )


def test_track_record_page(seeded):
    import base64
    import html

    from fastapi.testclient import TestClient

    from desk.web.app import create_app

    auth = {"Authorization": "Basic " + base64.b64encode(b"u:p").decode()}
    with session_scope(seeded) as s:
        _decision(s, _inst(s, "VUSA"), dt.date(2026, 7, 20), "BUY", 0.05, status="executed")
    with TestClient(create_app(seeded)) as c:
        body = html.unescape(c.get("/track-record", headers=auth).text)
        for needle in (
            "Hit rate",
            "Promotion checklist",
            "The system stays paper",
            "Screener track record",
            "Seeded August ideas",
            "S&P 500 Dec-26",
        ):
            assert needle in body, needle
