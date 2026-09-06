"""Screener and decision review of 2026-09-06: Safra sector-only cap, portfolio fit on the real gaps,
crowd 'inputs incomplete', fundamentals gate, sector cap with overflow, tiebreaks, sizing math, drafted kill
conditions, Alpha Vantage budget order."""

from __future__ import annotations

import datetime as dt

import pytest
from sqlmodel import select

from desk.crowd import crowd_factor
from desk.db import session_scope
from desk.decisions import run_pipeline
from desk.fixtures import load_fixtures
from desk.ingest.revolut import confirm_batch
from desk.kill_conditions import draft_kill_conditions
from desk.models import Decision, Instrument, Observation, ScreenerRow
from desk.portfolio import Limits, build_portfolio
from desk.score import SECTOR_ONLY_CAP, Factor, ScoreResult, score_universe
from desk.screener import (
    gate_reason,
    rank_key,
    refresh_constituents,
    run_screener,
    select_top,
    sentiment_targets,
    tiebreak_notes,
)
from desk.seed import load_all_seeds
from desk.universe import sync_instruments
from tests.conftest import load_fixture

TODAY = dt.date(2026, 9, 6)


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


def _scores(s, settings):
    from desk.regime import latest_regime

    view = build_portfolio(s, settings)
    return {
        r.row.instrument_id: r
        for r in score_universe(s, view, latest_regime(s), settings=settings, today=TODAY)
    }


def test_portfolio_fit_measures_the_real_gaps(seeded):
    with session_scope(seeded) as s:
        res = _scores(s, seeded)
        view = build_portfolio(s, seeded)
        limits = Limits.load(seeded.config_dir / "limits.yaml")
        core = sum(v for k, v in view.by_theme.items() if k in limits.core_themes)
        assert core < limits.min_diversified_core_warn  # the book's real problem
        vwce, nvda, pot, exw1 = (_inst(s, t) for t in ("VWCE", "NVDA", "COMMODITIES_POT", "EXW1"))
        pf = res[vwce.id].factors["portfolio"]
        assert pf.inputs["base"] == 5.0 and "below the 25% floor" in pf.inputs["why"]
        assert res[exw1.id].factors["portfolio"].inputs["base"] == 5.0
        pn = res[nvda.id].factors["portfolio"]
        assert pn.inputs["base"] == 1.0 and "single + private" in pn.inputs["why"]
        assert pn.inputs["single_plus_private_weight"] > limits.max_single_and_private
        assert res[pot.id].factors["portfolio"].inputs["base"] == 1.0  # gold > 35%
        # correlation is measured against the two largest holdings through their proxies, never ORA
        corr = pn.inputs["correlations"]
        assert [c["holding"] for c in corr] == ["COMMODITIES_POT", "WHOOP"]
        assert corr[0]["proxy"] == "GC=F" and corr[0]["corr_90d"] is not None
        assert "no price series" in corr[1]["note"]
        assert res[vwce.id].row.total > res[nvda.id].row.total
        assert res[exw1.id].row.total > res[nvda.id].row.total


def test_sector_only_safra_view_is_capped_for_single_stocks(seeded):
    with session_scope(seeded) as s:
        doc = load_fixture("constituents.json")
        refresh_constituents(s, seeded, fetch=lambda src: doc.get(src, []))
        res = _scores(s, seeded)
        nvda = _inst(s, "NVDA")
        assert res[nvda.id].factors["safra"].value == 5.0  # named focus-list buy keeps 5
        from desk.houseviews import all_views
        from desk.regime import latest_regime
        from desk.regime_fit import RegimeFit
        from desk.score import compute_score

        msft = _inst(s, "MSFT")
        r = compute_score(
            s,
            msft,
            regime=latest_regime(s),
            fit=RegimeFit.load(seeded.config_dir / "regime_fit.yaml"),
            view=build_portfolio(s, seeded),
            views=all_views(s),
            universe_returns={},
            valuation_cfg=None,
            today=TODAY,
        )
        f = r.factors["safra"]
        assert (
            "stock_rating" not in f.inputs
            and f.value <= SECTOR_ONLY_CAP
            and "capped" in f.inputs.get("cap", "")
        )


def test_crowd_incomplete_positioning_defaults_to_three_with_a_note(settings):
    with session_scope(settings) as s:
        sync_instruments(s)
        for i in range(12):
            s.add(
                Observation(
                    series="COT:SP500",
                    date=TODAY - dt.timedelta(weeks=i),
                    value=float(i * 1000),
                    source="test",
                    fetched_at=dt.datetime(2026, 9, 6),
                )
            )
        s.commit()
        res = crowd_factor(s, _inst(s, "VUSA"), TODAY)
        assert res.value == 3.0 and res.incomplete and res.percentile is None
        assert "positioning inputs incomplete" in res.note and "COT:SP500" in res.note


def test_crowd_basis_lists_its_inputs(seeded):
    with session_scope(seeded) as s:
        res = crowd_factor(s, _inst(s, "VUSA"), TODAY)
        assert "inputs:" in res.note and "CNN_PUTCALL_5D" in res.note and "missing:" in res.note


def _fake(ticker, sector, total, val, crowd, passed=True, reason=None):
    inst = Instrument(
        id=hash(ticker) % 10_000,
        ticker=ticker,
        name=ticker,
        kind="stock",
        currency="USD",
        sector=sector,
    )
    from desk.models import Score

    row = Score(
        instrument_id=inst.id,
        date=TODAY,
        total=total,
        f_safra=0,
        f_regime=0,
        f_portfolio=0,
        f_valuation=val,
        f_momentum=0,
        f_season=0,
        f_crowd=crowd,
        inputs_json={},
    )
    res = ScoreResult(row, {"valuation": Factor(val), "crowd": Factor(crowd)}, False, [])
    gates = {
        "passed": passed,
        "quality_reasons": [reason] if reason else [],
        "no_earnings": False,
        "value_trap": False,
    }
    gates["reason"] = gate_reason(gates)
    return inst, res, gates


def test_ranking_tiebreak_sector_cap_and_overflow():
    rows = [
        _fake("A1", "IT", 76.2, 2.0, 3.0),
        _fake("A2", "IT", 76.4, 3.5, 3.0),  # same displayed 76: valuation wins
        _fake("A3", "IT", 76.0, 3.5, 4.0),  # valuation equal to A2: crowd wins
        _fake("B1", "IT", 75.0, 1.0, 1.0),
        _fake(
            "B2",
            "IT",
            75.0,
            1.0,
            1.0,
            passed=False,
            reason="no fundamentals (weekly job has not populated this name)",
        ),
        _fake("B3", "IT", 74.0, 1.0, 1.0),
        _fake("B4", "IT", 73.0, 1.0, 1.0),  # sixth IT name -> over the cap
        _fake("C1", "Industrials", 72.0, 1.0, 1.0),
        _fake("D1", "Energy", 50.0, 1.0, 1.0),
    ]
    rows.sort(key=rank_key)
    assert [i.ticker for i, _, _ in rows[:3]] == ["A3", "A2", "A1"]
    notes = tiebreak_notes(rows)
    assert notes[rows[1][0].id].startswith("tie at 76: valuation equal, crowd 3.00 vs 4.00")
    assert notes[rows[2][0].id].startswith("tie at 76: valuation 2.00 vs 3.50")
    top, overflow, excluded = select_top(rows, 15, 5)
    assert [i.ticker for i, _, _ in top] == ["A3", "A2", "A1", "B1", "B3", "C1", "D1"]
    assert excluded[rows[4][0].id].startswith("gated out: no fundamentals")
    assert "sector cap: IT already has 5" in excluded[rows[6][0].id]
    assert [i.ticker for i, _, _ in overflow] == ["B2", "B4"]
    assert gate_reason({"passed": True}) is None
    assert gate_reason(
        {"passed": False, "quality_reasons": ["revenue growth not positive"], "value_trap": True}
    ) == ("revenue growth not positive; possible value trap")


def test_screener_run_writes_lists_and_sentiment_order(seeded):
    with session_scope(seeded) as s:
        doc = load_fixture("constituents.json")
        refresh_constituents(s, seeded, fetch=lambda src: doc.get(src, []))
        res = run_screener(s, seeded, TODAY)
        assert res["scored"] > 400 and len(res["top"]) == 15
        assert max(res["sectors"].values()) <= 5 and sum(res["sectors"].values()) == 15
        rows = s.exec(select(ScreenerRow).where(ScreenerRow.date == TODAY)).all()
        lists = {(r.factors_json or {}).get("list") for r in rows}
        assert {"top", "bottom"} <= lists
        top_rows = [r for r in rows if r.factors_json.get("list") == "top"]
        assert all(r.gates_json["passed"] for r in top_rows)
        assert sorted(r.factors_json["list_rank"] for r in top_rows) == list(range(1, 16))
        for r in rows:
            if r.factors_json.get("list") == "overflow":
                assert r.factors_json["excluded"].startswith(("gated out:", "sector cap:"))
        # a stock without fundamentals says so and is gated out
        nofund = next((r for r in rows if not r.gates_json.get("fundamentals")), None)
        if nofund is not None:
            assert "no fundamentals" in nofund.gates_json["reason"]
        # budget order: held US single names first, then the screener's top 20 by rank
        order = sentiment_targets(s, seeded, TODAY)
        assert order[:2] == ["TSLA", "NVDA"] or order[0] == "TSLA"
        ranked = s.exec(
            select(ScreenerRow).where(ScreenerRow.date == TODAY).order_by(ScreenerRow.rank)
        ).all()
        first_screener = next(
            s.get(Instrument, r.instrument_id).ticker for r in ranked if r.rank == 1
        )
        assert first_screener in order and order.index(first_screener) >= order.index("TSLA")
        assert len(sentiment_targets(s, seeded, TODAY, budget=3)) == 3


def test_decision_shows_sizing_math_and_drafted_kill_conditions(seeded, monkeypatch):
    from desk import score as score_mod

    monkeypatch.setitem(
        score_mod.BANDS, "act", 65
    )  # after the portfolio-fit rewrite nothing reaches 75 today
    with session_scope(seeded) as s:
        out = run_pipeline(s, seeded, today=TODAY)
        buys = s.exec(
            select(Decision).where(Decision.date == TODAY, Decision.action == "BUY")
        ).all()
        assert buys, out.notes
        d = buys[0]
        inst = s.get(Instrument, d.instrument_id)
        sizing = d.rules_json["sizing"]
        assert d.size_pct == pytest.approx(
            min(
                sizing["cash available"],
                sizing["single-position headroom"],
                sizing["theme headroom"],
                sizing["per-decision cap"],
            )
        )
        assert "= min(cash" in d.reasoning_md and "per-decision cap 5.0%" in d.reasoning_md
        kj = d.rules_json["kill_json"]
        assert kj["kills"], (
            "a BUY always carries a kill block (seed entry or drafted from the template)"
        )
        if kj.get("drafted"):
            preds = [k.get("predicate", "") for k in kj["kills"]]
            assert any(p.startswith(f"close('{inst.ticker}') <") for p in preds)
        assert "Basis" in d.reasoning_md and "| crowd |" in d.reasoning_md
        assert (
            "| valuation | " in d.reasoning_md
            and " — |" not in d.reasoning_md.split("| valuation |")[1].split("\n")[0]
        )


def test_draft_kill_conditions_for_a_focus_list_name(seeded):
    with session_scope(seeded) as s:
        nvda = _inst(s, "NVDA")
        kill = draft_kill_conditions(s, nvda, 230.36, 0.18, seeded)
        preds = {k.get("predicate") for k in kill["kills"] if k.get("predicate")}
        humans = [k["human"] for k in kill["kills"] if k.get("human")]
        assert "close('NVDA') < 188.90" in preds
        assert any("house_view('sector', 'Information Technology').stance !=" in p for p in preds)
        assert any("house_view('stock', 'NVDA').stance != 'buy'" in p for p in preds)
        assert any("capex guidance cut" in h for h in humans)
        assert any("Score below 45" in h for h in humans)
