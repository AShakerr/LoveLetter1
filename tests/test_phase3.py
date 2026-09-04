"""Regime classifier, predicate DSL, regime-fit loader, scores, rules, decisions, paper broker."""

import datetime as dt
from pathlib import Path

import pytest
from sqlmodel import select

from desk.broker import PaperBroker, RevolutBroker
from desk.db import session_scope
from desk.decisions import respond, run_pipeline
from desk.fixtures import load_fixtures
from desk.ingest.revolut import confirm_batch
from desk.models import Decision, Instrument, PaperPosition, Position, Regime, RuleFired, Score
from desk.portfolio import build_portfolio
from desk.predicates import Context, PredicateError, evaluate
from desk.regime import classify, make_label
from desk.regime_fit import RegimeFit
from desk.rules import run_rules
from desk.score import band, score_universe
from desk.seed import load_all_seeds
from desk.universe import sync_instruments

TODAY = dt.date(2026, 9, 4)


@pytest.fixture
def seeded(settings):
    load_fixtures(settings)
    with session_scope(settings) as s:
        sync_instruments(s)
        load_all_seeds(s, settings)
        confirm_batch(s, "seed:positions_2026-09-04")
    return settings


def _inst(s, ticker):
    return s.exec(select(Instrument).where(Instrument.ticker == ticker)).one()


# ---------------------------------------------------------------------------------------------- regime
def test_regime_classifier(seeded):
    with session_scope(seeded) as s:
        r = classify(s, TODAY)
        assert r.inflation_state == "energy_shock"  # EZ HICP 3.3 vs core 2.5 in the fixtures
        assert r.vol_state == "complacent"  # VIX 14.3
        assert r.policy_state in ("hiking", "on_hold", "cutting")
        assert r.oil_state in ("shock", "elevated", "normal")
        assert r.label == make_label(r.inflation_state, r.policy_state, r.oil_state, r.vol_state)
        assert r.inputs_json["inflation"]["ez"]["headline_yoy"] == 3.3
        assert r.inputs_json["policy"]["house_forecasts"]
        # idempotent upsert
        assert len(s.exec(select(Regime).where(Regime.date == TODAY)).all()) == 1


# -------------------------------------------------------------------------------------------- predicates
def test_predicate_dsl(seeded):
    with session_scope(seeded) as s:
        tsla = _inst(s, "TSLA")
        pos = s.exec(select(Position).where(Position.instrument_id == tsla.id)).one()
        ctx = Context(s, tsla, pos, {"gold": 0.62}, TODAY)
        assert evaluate("house_view(sector).stance == neutral", ctx) is True
        assert evaluate("house_view('Energy').stance == least_preferred", ctx) is True
        assert evaluate("house_view(region).stance == 'least_preferred'", ctx) is False
        assert evaluate("observation('BZ=F') > 80 and observation('EZ_HICP') > 3", ctx) is True
        assert evaluate("observation('DXY') > 50", ctx) is True
        assert evaluate("observation('ECB_DEPO') >= 2.25", ctx) is True
        assert evaluate("close() < avg_cost() * 0.5", ctx) is False
        assert evaluate("close('TSLA') > 0", ctx) is True
        assert evaluate("theme_weight(gold) > 35", ctx) is True
        assert evaluate("theme_weight('private_space') > 15", ctx) is False
        assert evaluate("days_since('2026-06-11') > 30", ctx) is True
        assert evaluate("days_since('EZ_HICP') > 30", ctx) is True  # monthly series dated 1 July
        assert evaluate("days_since('BZ=F') <= 1", ctx) is True
        assert evaluate("not (change_pct('GC=F', 60) < -50)", ctx) is True
        assert evaluate("house_view('sector', 'Materials').stance == 'most_preferred'", ctx) is True
        assert evaluate("house_view('commodity', 'Gold Dec-26').value < 4470", ctx) is False
        assert (
            evaluate("house_view('region', 'Emerging Markets').stance != 'least_preferred'", ctx)
            is True
        )
        assert evaluate("close('TSLA') < 0.82 * avg_cost('TSLA')", ctx) is False
        assert evaluate("observation('DGS30') > 5.50", ctx) is False
        with pytest.raises(PredicateError):
            evaluate("change_pct('SPCX', 60) < -25", ctx)  # no price history for the private line
        with pytest.raises(PredicateError):
            evaluate("house_view('region', 'Materials').stance == neutral", ctx)  # wrong scope
        assert evaluate("sentiment('TSLA', 14) > -1", ctx) is True
        assert evaluate("house_view('S&P 500 Dec-26').value >= 8200", ctx) is True
        for bad in (
            "import os",
            "__import__('os')",
            "house_view('Nope').stance == neutral",
            "observation('NOPE') > 1",
            "close('TSLA')",
            "avg_cost('VWCE') > 1",
            "sentiment('VWCE') > 0",
            "foo() > 1",
            "1 +",
            "[1,2]",
        ):
            with pytest.raises(PredicateError):
                evaluate(bad, ctx)


def test_regime_fit_from_config(settings):
    fit = RegimeFit.load(settings.config_dir / "regime_fit.yaml")
    assert fit is not None and fit.reverse_scenario == {
        "inflation_state": "contained",
        "policy_state": "on_hold",
        "oil_state": "normal",
        "vol_state": "normal",
    }
    regime = Regime(
        date=TODAY,
        label="x",
        inflation_state="energy_shock",
        policy_state="hiking",
        oil_state="shock",
        vol_state="complacent",
    )
    v, inputs = fit.score("energy", regime)
    assert v == pytest.approx(0.6 * (5 + 3 + 5 + 3) / 4 + 0.4 * (1 + 3 + 1 + 3) / 4)
    v_gold, _ = fit.score("gold", regime)
    assert v_gold == pytest.approx(0.6 * (3 + 1 + 3 + 2) / 4 + 0.4 * (2 + 3 + 2 + 3) / 4)
    assert fit.score("cash", regime)[0] is None


def test_regime_fit_loader(tmp_path: Path):
    (tmp_path / "regime_fit.yaml").write_text("""
reverse_scenario: {inflation_state: contained, policy_state: cutting, oil_state: normal, vol_state: normal}
themes:
  gold:
    inflation_state: {energy_shock: 4, broad: 4, contained: 2}
    policy_state: {hiking: 2, on_hold: 3, cutting: 4}
    oil_state: {shock: 3, elevated: 3, normal: 3}
    vol_state: {complacent: 2, normal: 3, stressed: 4}
""")
    fit = RegimeFit.load(tmp_path / "regime_fit.yaml")
    regime = Regime(
        date=TODAY,
        label="x",
        inflation_state="energy_shock",
        policy_state="hiking",
        oil_state="shock",
        vol_state="complacent",
    )
    v, inputs = fit.score("gold", regime)
    cur = (4 + 2 + 3 + 2) / 4
    rev = (2 + 4 + 3 + 3) / 4
    assert v == pytest.approx(0.6 * cur + 0.4 * rev)
    assert inputs["formula"] == "0.6*current + 0.4*reverse"
    assert fit.score("unknown-theme", regime)[0] is None
    assert RegimeFit.load(tmp_path / "missing.yaml") is None


# ------------------------------------------------------------------------------------------------ score
def test_scores(seeded):
    with session_scope(seeded) as s:
        view = build_portfolio(s, seeded)
        regime = classify(s, TODAY)
        results = {
            r.row.instrument_id: r
            for r in score_universe(s, view, regime, settings=seeded, today=TODAY)
        }
        tsla, nvda, vusa, pot = (_inst(s, t) for t in ("TSLA", "NVDA", "VUSA", "COMMODITIES_POT"))
        assert not results[tsla.id].provisional
        assert results[tsla.id].factors["regime"].value is not None
        assert results[tsla.id].factors["regime"].inputs["formula"] == "0.6*current + 0.4*reverse"
        assert all(0 <= r.row.total <= 100 for r in results.values())
        # sector Consumer Discretionary neutral (2.5), region USA most preferred (5), Equities overweight (5)
        assert results[tsla.id].factors["safra"].value == pytest.approx((2.5 + 5 + 5) / 3)
        assert results[nvda.id].factors["safra"].value == 5.0  # focus-list buy overrides
        assert results[nvda.id].factors["valuation"].inputs["target"]["target"] == 270
        assert results[vusa.id].factors["valuation"].inputs["target"]["key"] == "S&P 500 Dec-26"
        assert (
            results[vusa.id].factors["valuation"].inputs["pe"]["n"] == 3
        )  # three Safra performance tables
        assert results[pot.id].factors["portfolio"].value <= 1.0  # gold already 62%
        assert results[vusa.id].factors["portfolio"].inputs["base"] == 5.0
        assert all(r.factors["season"].value == 0 for r in results.values())  # September
        assert len(s.exec(select(Score).where(Score.date == TODAY)).all()) == len(results)
        # second run upserts, no duplicates
        score_universe(s, view, regime, settings=seeded, today=TODAY)
        assert len(s.exec(select(Score).where(Score.date == TODAY)).all()) == len(results)
    assert (
        band(80) == "act"
        and band(60) == "candidate"
        and band(45) == "watch"
        and band(44.9) == "avoid"
    )


# ------------------------------------------------------------------------------------------------ rules
def test_rules_composition_and_mandatory(seeded):
    with session_scope(seeded) as s:
        view = build_portfolio(s, seeded)
        regime = classify(s, TODAY)
        scores = {
            r.row.instrument_id: r
            for r in score_universe(s, view, regime, settings=seeded, today=TODAY)
        }
        flags = run_rules(s, view, scores, settings=seeded, today=TODAY)
        by_rule = {}
        for f in flags:
            by_rule.setdefault(f.rule, []).append(f)
        pot = _inst(s, "COMMODITIES_POT")
        # unconfirmed composition: REVIEW, not TRIM
        mp = [f for f in by_rule["max_position"] if f.instrument_id == pot.id]
        assert (
            mp
            and mp[0].severity == "review"
            and "confirm pot composition" in mp[0].summary
            and mp[0].action is None
        )
        mt = [f for f in by_rule["max_theme"] if f.instrument_id == pot.id]
        assert mt and mt[0].severity == "review"
        assert by_rule["concentration_warning"][0].severity == "review"
        assert not any(
            f.rule == "stop_loss" for f in flags
        )  # nothing 18%/12% under cost in the seed
        assert len(s.exec(select(RuleFired).where(RuleFired.date == TODAY)).all()) == len(flags)
        # confirm composition -> mandatory TRIM with sizes
        pot.composition_confirmed = True
        s.add(pot)
        s.commit()
        flags = run_rules(s, view, scores, settings=seeded, today=TODAY)
        mp = [f for f in flags if f.rule == "max_position" and f.instrument_id == pot.id][0]
        assert (
            mp.severity == "mandatory"
            and mp.action == "TRIM"
            and mp.size_pct == pytest.approx(mp.detail["weight"] - 0.15)
        )
        mt = [f for f in flags if f.rule == "max_theme" and f.instrument_id == pot.id][0]
        assert mt.severity == "mandatory" and mt.action == "TRIM"
        pot_thesis = [
            f for f in flags if f.rule == "thesis_invalidated" and f.instrument_id == pot.id
        ][0]
        assert pot_thesis.severity == "mandatory" and pot_thesis.action == "SELL"


def test_rules_stop_loss_and_thesis(seeded):
    with session_scope(seeded) as s:
        tsla = _inst(s, "TSLA")
        pos = s.exec(select(Position).where(Position.instrument_id == tsla.id)).one()
        pos.avg_cost = 1000.0  # price ~352 -> far below an 18% stop
        pos.kill_condition = "Thesis: Safra keeps Consumer Discretionary at least neutral"
        pos.kill_predicate = "house_view(sector).stance == least_preferred"
        pos.kill_json = None
        s.add(pos)
        vusa = _inst(s, "VUSA")
        vpos = s.exec(select(Position).where(Position.instrument_id == vusa.id)).one()
        vpos.kill_condition = "Thesis text that the DSL cannot check"
        vpos.kill_predicate = "observation('NOT_A_SERIES') < 1"
        vpos.kill_json = None
        s.add(vpos)
        x9 = _inst(s, "X9I1")
        xpos = s.exec(select(Position).where(Position.instrument_id == x9.id)).one()
        xpos.kill_condition = "EM thesis dies if EZ inflation stays above 1%"
        xpos.kill_json = {
            "thesis": xpos.kill_condition,
            "kills": [
                {
                    "predicate": "observation('EZ_HICP') > 1",
                    "severity": "mandatory",
                    "note": "true today",
                },
                {"predicate": "observation('DXY') > 90", "severity": "review", "note": "also true"},
                {"human": "Check the fund's swap counterparty", "severity": "review"},
            ],
        }
        s.add(xpos)
        s.commit()
        view = build_portfolio(s, seeded)
        regime = classify(s, TODAY)
        scores = {
            r.row.instrument_id: r
            for r in score_universe(s, view, regime, settings=seeded, today=TODAY)
        }
        flags = run_rules(s, view, scores, settings=seeded, today=TODAY)
        sl = [f for f in flags if f.rule == "stop_loss"]
        assert (
            len(sl) == 1
            and sl[0].instrument_id == tsla.id
            and sl[0].severity == "mandatory"
            and sl[0].action == "SELL"
        )
        assert sl[0].detail["stop_pct"] == 0.18
        assert not any(f.rule == "thesis_invalidated" and f.instrument_id == tsla.id for f in flags)
        un = [f for f in flags if f.rule == "thesis_unevaluable" and f.instrument_id == vusa.id]
        assert len(un) == 1 and un[0].severity == "review"
        assert "Thesis text that the DSL cannot check" in un[0].summary
        inv = [f for f in flags if f.rule == "thesis_invalidated" and f.severity == "mandatory"]
        assert (
            len(inv) == 1
            and inv[0].instrument_id == x9.id
            and inv[0].action == "SELL"
            and "true today" in inv[0].summary
        )
        assert any(f.rule == "thesis_review" and f.instrument_id == x9.id for f in flags)
        assert any(
            f.rule == "thesis_human"
            and f.instrument_id == x9.id
            and "swap counterparty" in f.summary
            for f in flags
        )


# -------------------------------------------------------------------------------------- decisions/paper
def test_pipeline_decisions_idempotent_and_paper(seeded):
    with session_scope(seeded) as s:
        res = run_pipeline(s, seeded, TODAY)
        assert res.created > 0
        actions = {(s.get(Instrument, d.instrument_id).ticker, d.action) for d in res.decisions}
        held = {"TSLA", "VUSA", "X9I1", "4COP", "SPCX", "COMMODITIES_POT"}
        for t in held:
            assert any(tk == t for tk, _ in actions), t
        # unconfirmed pot -> HOLD with review flags, never TRIM
        pot_d = next(
            d
            for d in res.decisions
            if s.get(Instrument, d.instrument_id).ticker == "COMMODITIES_POT"
        )
        assert pot_d.action == "HOLD"
        assert any(
            f["rule"] == "max_position" and f["severity"] == "review"
            for f in pot_d.rules_json["flags"]
        )
        rules_section = pot_d.reasoning_md.split("## Rules")[1].split("## Kill condition")[0]
        assert "Regime" in pot_d.reasoning_md
        assert "MANDATORY" not in rules_section and "REVIEW" in rules_section
        assert "What would reverse this" in pot_d.reasoning_md
        assert not any("provisional" in n for n in res.notes)
        assert "## Kill condition" in pot_d.reasoning_md and "Gold Dec-26" in pot_d.reasoning_md
        assert all(d.user_status == "pending" for d in res.decisions)
        # rerun the same day: nothing new
        again = run_pipeline(s, seeded, TODAY)
        assert again.created == 0
        assert len(s.exec(select(Decision)).all()) == res.created
        # paper book seeded from the confirmed positions
        assert PaperBroker().is_seeded(s)
        rows = {r["ticker"]: r for r in PaperBroker().compare(s, res.view)}
        actionable = {
            s.get(Instrument, d.instrument_id).ticker
            for d in res.decisions
            if d.action in ("BUY", "ADD", "TRIM", "SELL")
        }
        # the paper book only diverges where the system acted (plus cash, which funds the buys)
        for t, r in rows.items():
            if t not in actionable and t != "CASH_USD":
                assert abs(r["diff_eur"]) < 1, (t, r)
        assert res.paper_fills == len(actionable)
        for t in actionable:
            assert abs(rows[t]["diff_eur"]) > 1, t
        # a held instrument never gets both a HOLD and an ADD on the same day
        per_inst = {}
        for d in res.decisions:
            per_inst.setdefault(d.instrument_id, set()).add(d.action)
        assert not any({"HOLD", "ADD"} <= acts for acts in per_inst.values())


def test_execute_sell_and_buy_updates_positions(seeded):
    with session_scope(seeded) as s:
        tsla = _inst(s, "TSLA")
        pos = s.exec(select(Position).where(Position.instrument_id == tsla.id)).one()
        pos.avg_cost = 1000.0
        s.add(pos)
        s.commit()
        res = run_pipeline(s, seeded, TODAY)
        sell = next(d for d in res.decisions if d.instrument_id == tsla.id)
        assert sell.action == "SELL" and sell.size_pct == pytest.approx(
            res.view.positions
            and next(p.weight for p in res.view.positions if p.instrument.id == tsla.id)
        )
        assert "stop_loss" in sell.reasoning_md and "MANDATORY" in sell.reasoning_md
        # the paper broker sold it already
        paper = s.exec(select(PaperPosition).where(PaperPosition.instrument_id == tsla.id)).one()
        assert paper.quantity == 0
        # user executes: position closes, decision text untouched
        text = sell.reasoning_md
        respond(s, sell, "executed", "sold at 350", seeded)
        assert sell.user_status == "executed" and sell.executed_at and sell.reasoning_md == text
        assert (
            s.exec(
                select(Position).where(
                    Position.instrument_id == tsla.id, Position.closed_at.is_(None)
                )
            ).first()
            is None
        )
        # a synthetic BUY executes into a new confirmed position carrying the kill condition
        nvda = _inst(s, "NVDA")
        view_total = build_portfolio(s, seeded).total_eur
        buy = Decision(
            date=TODAY,
            instrument_id=nvda.id,
            action="BUY",
            size_pct=0.05,
            score_id=None,
            rules_json={
                "kill_condition": "AI capex thesis",
                "kill_predicate": "house_view(sector).stance == least_preferred",
            },
            reasoning_md="# BUY NVDA",
            created_at=dt.datetime.utcnow(),
        )
        s.add(buy)
        s.commit()
        respond(s, buy, "executed", None, seeded)
        newpos = s.exec(
            select(Position).where(Position.instrument_id == nvda.id, Position.closed_at.is_(None))
        ).one()
        assert (
            newpos.confirmed_by_user
            and newpos.kill_predicate
            and newpos.batch == f"decision:{buy.id}"
        )
        assert newpos.quantity * newpos.avg_cost == pytest.approx(
            0.05 * view_total * build_portfolio(s, seeded).fx["USD"].per_eur, rel=0.02
        )
        with pytest.raises(ValueError):
            respond(s, buy, "bogus")


def test_revolut_stub():
    with pytest.raises(NotImplementedError):
        RevolutBroker().positions(None)


def test_seed_kill_conditions_loaded(seeded):
    from desk.kill_conditions import candidate_conditions, condition_for

    with session_scope(seeded) as s:
        for t, n_kills in (
            ("COMMODITIES_POT", 3),
            ("SPCX", 3),
            ("TSLA", 2),
            ("X9I1", 3),
            ("4COP", 3),
            ("VUSA", 2),
            ("ROBO_ADVISOR", 0),
        ):
            pos = s.exec(select(Position).where(Position.instrument_id == _inst(s, t).id)).one()
            assert pos.kill_json is not None and len(pos.kill_json["kills"]) == n_kills, t
        pot = s.exec(
            select(Position).where(Position.instrument_id == _inst(s, "COMMODITIES_POT").id)
        ).one()
        assert pot.kill_predicate == "house_view('commodity', 'Gold Dec-26').value < 4470"
        assert "composition_confirmed" in pot.kill_json["pre_condition"]
        cop = s.exec(select(Position).where(Position.instrument_id == _inst(s, "4COP").id)).one()
        assert cop.kill_json["add_blocked_while"].startswith("theme_weight('gold')")
        cands = candidate_conditions(seeded)
        assert set(cands) == {"EU_BROAD_ETF", "INDUSTRIALS_ETF"}
        assert condition_for(_inst(s, "EXW1"), cands)["theme"] == "eu_broad"
        assert condition_for(_inst(s, "ZPDI"), cands)["kills"][1]["human"].startswith(
            "Two or more hyperscalers"
        )
        assert condition_for(_inst(s, "TSLA"), cands) is None


def test_add_blocked_while(seeded):
    from desk.rules import add_blocked

    with session_scope(seeded) as s:
        view = build_portfolio(s, seeded)
        cop = next(p for p in view.positions if p.instrument.ticker == "4COP")
        assert add_blocked(s, cop, view, TODAY).startswith(
            "theme_weight('gold')"
        )  # gold 62% + copper > 35
        tsla = next(p for p in view.positions if p.instrument.ticker == "TSLA")
        assert add_blocked(s, tsla, view, TODAY) is None
