"""7b crowd, 7c valuation, 8b execution and guards, 8c screener."""

import datetime as dt

import pytest
from sqlmodel import select

from desk.broker.base import Costs, Order
from desk.broker.guards import GuardError, engage_kill_switch, release_kill_switch
from desk.crowd import (
    consensus_gap,
    crowd_long_percentile,
    deferral_reason,
    range_percentile,
    score_crowd,
)
from desk.db import session_scope
from desk.decisions import respond, run_pipeline
from desk.events import load_events_config, surprise_direction, trading_days_ahead, upcoming
from desk.execution import paper_vs_actual, settle_paper, submit_decision
from desk.fixtures import load_fixtures
from desk.ingest.revolut import confirm_batch
from desk.models import (
    Decision,
    Event,
    FillRow,
    Fundamental,
    Instrument,
    Observation,
    OrderRow,
    Position,
    Price,
    RuleFired,
    ScreenerRow,
)
from desk.portfolio import build_portfolio
from desk.screener import (
    ScreenerConfig,
    days_in_top,
    page_rows,
    propose_buy,
    refresh_constituents,
    run_screener,
)
from desk.seed import load_all_seeds
from desk.universe import sync_instruments
from desk.valuation import peg_band, quality_gate, score_etf, score_stock, value_trap, z_band

TODAY = dt.date(2026, 9, 4)
EARLY = dt.date(2026, 9, 1)  # a date with a next session in the fixtures, so paper orders fill


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


# ------------------------------------------------------------------------------------- 7c valuation
def test_valuation_bands():
    assert [peg_band(x)[0] for x in (0.8, 1.0, 1.2, 1.7, 2.5, 3.5)] == [5, 4, 4, 3, 2, 1]
    assert peg_band(None) == (0.0, "no earnings") and peg_band(-1)[1] == "no earnings"
    assert [z_band(z) for z in (-1.5, -0.5, 0.0, 0.5, 1.5)] == [5, 4, 3, 2, 1]
    sector = {"median": 20.0, "std": 5.0, "n": 30}
    cheap = {
        "pegRatio": 0.9,
        "forwardPE": 12.0,
        "trailingPE": 14.0,
        "revenueGrowth": 0.1,
        "earningsGrowth": 0.1,
    }
    r = score_stock(cheap, sector, (None, 0))
    # PEG 5, sector z=-1.6 -> 5, history unavailable -> sector twice -> mean 5
    assert (
        r.value == 5.0
        and r.inputs["components"] == {"peg": 5.0, "sector": 5.0, "history": 5.0}
        and not r.flags
    )
    with_hist = score_stock(
        cheap, sector, (10.0, 60)
    )  # trailing 14 vs own median 10 -> z +2.7 -> 1
    assert with_hist.inputs["components"]["history"] == 1.0 and with_hist.value == pytest.approx(
        (5 + 5 + 1) / 3
    )
    noearn = score_stock({"pegRatio": None, "forwardPE": 25.0}, sector, (None, 0))
    assert noearn.total_cap == 60 and "no earnings" in noearn.flags
    trap = score_stock(
        {"pegRatio": 0.5, "forwardPE": 9.0, "trailingPE": 7.0, "earningsGrowth": -0.109},
        sector,
        (None, 0),
    )
    assert trap.value == 2.0 and any("value trap" in f for f in trap.flags)
    assert value_trap({"trailingPE": 7.0, "forwardPE": 8.0}) is not None
    assert (
        value_trap(
            {"trailingPE": 7.0, "forwardPE": 6.0, "earningsGrowth": 0.1, "revenueGrowth": 0.1}
        )
        is None
    )
    etf = score_etf(
        21.5, [(dt.date(2026, 8, d), v) for d, v in ((14, 21.8), (21, 21.3), (28, 21.5))]
    )
    assert etf.value == 3.0 and etf.inputs["median_5y"] == 21.5


def test_quality_gate():
    good = {
        "freeCashflow": 1e9,
        "totalDebt": 2e9,
        "totalCash": 5e8,
        "ebitda": 1e9,
        "revenueGrowth": 0.05,
        "numberOfAnalystOpinions": 12,
    }
    assert quality_gate(good, "Industrials") == (True, [])
    bad = {
        "freeCashflow": -1.0,
        "totalDebt": 5e9,
        "totalCash": 0,
        "ebitda": 1e9,
        "revenueGrowth": -0.02,
        "numberOfAnalystOpinions": 3,
    }
    ok, reasons = quality_gate(bad, "Industrials")
    assert not ok and len(reasons) == 4
    bank = {
        "freeCashflow": 1e9,
        "revenueGrowth": 0.05,
        "numberOfAnalystOpinions": 12,
    }  # leverage exempt
    assert quality_gate(bank, "Banks")[0]


# ----------------------------------------------------------------------------------------- 7b crowd
def test_crowd_bands():
    assert score_crowd(95, 0)[0] == 1 and score_crowd(5, 0)[0] == 4
    assert score_crowd(50, 0)[0] == 3 and score_crowd(50, 1)[0] == 4 and score_crowd(50, -1)[0] == 2
    assert score_crowd(80, 0)[0] == 2 and score_crowd(20, 0)[0] == 3
    assert score_crowd(None, 0)[0] is None


def test_crowd_from_fixtures(seeded):
    with session_scope(seeded) as s:
        p, inputs = range_percentile(s, "COT:GOLD", today=TODAY)
        assert (
            p is not None and p > 90 and inputs["n"] > 100
        )  # synthetic gold net length trends to a 3-year high
        pot = _inst(s, "COMMODITIES_POT")
        pp, _ = crowd_long_percentile(s, pot, TODAY)
        assert pp == pytest.approx(p)
        vusa = _inst(s, "VUSA")
        pe, inp = crowd_long_percentile(s, vusa, TODAY)
        assert pe is not None and "CBOE_PUTCALL_TOTAL" in inp and "AAII_BULL_BEAR_SPREAD" in inp
        assert range_percentile(s, "NOPE", today=TODAY)[0] is None
        # consensus gap: placeholder consensus is 0 -> no cap; a real consensus within 2% caps Safra at 4
        assert consensus_gap(s, "S&P 500 Dec-26", 8200.0, TODAY)[0] is False
        s.add(
            Observation(
                series="CONSENSUS_TARGET:S&P 500 Dec-26",
                date=TODAY,
                value=8100.0,
                source="manual",
                fetched_at=dt.datetime.utcnow(),
            )
        )
        s.commit()
        within, cg = consensus_gap(s, "S&P 500 Dec-26", 8200.0, TODAY)
        assert within and cg["gap_pct"] == pytest.approx(1.23, abs=0.01)


def test_events_and_deferral(seeded):
    with session_scope(seeded) as s:
        assert load_events_config(s, seeded) == 0  # already loaded by the fixtures; idempotent
        assert trading_days_ahead(dt.date(2026, 9, 4), 2) == dt.date(
            2026, 9, 8
        )  # Friday + 2 trading days = Tuesday
        vusa = _inst(s, "VUSA")
        r0 = deferral_reason(s, vusa, TODAY, 85)

        assert r0 and "US payrolls" in r0  # the config calendar has payrolls on 4 Sep itself

        for e in s.exec(select(Event).where(Event.name.like("US payrolls%"))).all():
            s.delete(e)

        s.commit()

        assert deferral_reason(s, vusa, TODAY, 85) is None  # nothing else within 2 trading days
        s.add(
            Event(
                date=dt.date(2026, 9, 7),
                name="FOMC decision (mock)",
                kind="central_bank",
                consensus=3.75,
                higher_is_good=False,
                favours=["us_broad"],
                hurts=[],
            )
        )
        s.commit()
        assert [e.name for e in upcoming(s, TODAY, 2)] == ["FOMC decision (mock)"]
        r = deferral_reason(s, vusa, TODAY, 85)
        assert r and "FOMC decision (mock)" in r and "percentile 85" in r
        assert deferral_reason(s, vusa, TODAY, 50) is None  # mid-range positioning never defers
        ev = Event(
            date=TODAY,
            name="CPI",
            kind="macro",
            consensus=3.4,
            actual=3.1,
            higher_is_good=False,
            favours=["us_broad"],
            hurts=["gold"],
        )
        assert (
            surprise_direction(ev, "us_broad") == 1
            and surprise_direction(ev, "gold") == -1
            and surprise_direction(ev, "energy") == 0
        )


def test_pipeline_creates_deferred_decision(seeded, monkeypatch):
    with session_scope(seeded) as s:
        s.add(
            Event(
                date=dt.date(2026, 9, 7),
                name="FOMC decision (mock)",
                kind="central_bank",
                consensus=3.75,
                higher_is_good=False,
                favours=["us_broad"],
                hurts=[],
            )
        )
        s.commit()
        monkeypatch.setattr(
            "desk.crowd.crowd_long_percentile",
            lambda session, inst, today=None: (15.0, {"mock": True}),
        )
        res = run_pipeline(s, seeded, TODAY)
        buys = [d for d in res.decisions if d.action in ("BUY", "ADD")]
        assert buys, "expected an ADD/BUY to defer"
        assert all(d.user_status == "deferred" and "FOMC" in (d.user_note or "") for d in buys)
        assert res.deferred == len(buys)
        assert not s.exec(
            select(OrderRow).where(OrderRow.decision_id.in_([d.id for d in buys]))
        ).all()
        # mandatory exits are never deferred: none today, but their status would be pending
        assert all(
            d.user_status == "pending" for d in res.decisions if d.action in ("SELL", "TRIM")
        )


# ------------------------------------------------------------------------------------- 8b execution
def test_costs_classes(seeded):
    costs = Costs.load(seeded.config_dir / "costs.yaml")
    with session_scope(seeded) as s:
        assert costs.spread_for(_inst(s, "VUSA")) == 5 and costs.spread_for(_inst(s, "TSLA")) == 10
        assert (
            costs.spread_for(_inst(s, "TSLA"), market_cap=1e9) == 25
            and costs.spread_for(_inst(s, "ORA")) == 25
        )
        assert costs.spread_for(_inst(s, "BTC-USD")) == 50


def test_order_model():
    with pytest.raises(ValueError):
        Order(decision_id=1, instrument_id=1, side="BUY", client_ref="x")
    with pytest.raises(ValueError):
        Order(decision_id=1, instrument_id=1, side="BUY", quantity=1, notional=1, client_ref="x")
    with pytest.raises(ValueError):
        Order(
            decision_id=1,
            instrument_id=1,
            side="BUY",
            quantity=1,
            order_type="LIMIT",
            client_ref="x",
        )


def _buy_decision(s, inst, on, size=0.05, ref=None):
    d = Decision(
        date=on,
        instrument_id=inst.id,
        action="BUY",
        size_pct=size,
        rules_json={"reference_price": ref},
        reasoning_md="# BUY",
        created_at=dt.datetime.utcnow(),
    )
    s.add(d)
    s.commit()
    s.refresh(d)
    return d


def test_paper_fill_next_open_with_costs_and_slippage(seeded):
    with session_scope(seeded) as s:
        run_pipeline(s, seeded, EARLY)  # seeds the paper book
        vusa = _inst(s, "VUSA")
        ref = s.exec(
            select(Price)
            .where(Price.instrument_id == vusa.id, Price.date <= EARLY)
            .order_by(Price.date.desc())
        ).first()
        d = _buy_decision(
            s, vusa, EARLY, 0.03, ref.close
        )  # 3%: fundable from USD cash after conversion
        row = submit_decision(s, d, settings=seeded, today=EARLY)
        assert (
            row.status == "submitted"
            and row.broker == "paper"
            and row.client_ref == f"desk-{d.id}-2026-09-01"
        )
        assert row.notional == pytest.approx(
            0.03 * build_portfolio(s, seeded).total_eur, rel=0.01
        )  # EUR instrument
        fills = settle_paper(s, seeded)
        assert len(fills) == 1
        f = fills[0]
        nxt = s.exec(
            select(Price)
            .where(Price.instrument_id == vusa.id, Price.date > EARLY)
            .order_by(Price.date)
        ).first()
        assert f.filled_at.date() == nxt.date and f.price == pytest.approx(nxt.open * (1 + 5 / 1e4))
        assert f.slippage_bps == pytest.approx((f.price / ref.close - 1) * 1e4)
        assert f.quantity == pytest.approx(row.notional / f.price)
        s.refresh(row)
        assert row.status == "filled"
        paper = s.exec(
            select(Position).where(
                Position.broker == "paper",
                Position.instrument_id == vusa.id,
                Position.closed_at.is_(None),
            )
        ).one()
        manual = s.exec(
            select(Position).where(
                Position.broker == "manual",
                Position.instrument_id == vusa.id,
                Position.closed_at.is_(None),
            )
        ).one()
        assert paper.quantity == pytest.approx(manual.quantity + f.quantity)
        cash_eur = s.exec(
            select(Position)
            .where(Position.broker == "paper")
            .join(Instrument)
            .where(Instrument.ticker == "CASH_EUR")
        ).first()
        cash_usd = s.exec(
            select(Position)
            .where(Position.broker == "paper")
            .join(Instrument)
            .where(Instrument.ticker == "CASH_USD")
        ).first()
        assert (
            cash_usd.quantity < 4240.79
        )  # the EUR purchase was funded by converting USD cash at the day's FX
        assert cash_eur is None or cash_eur.quantity == pytest.approx(0, abs=1e-6)
        # the paper book now diverges from actual only on VUSA (and cash)
        _, _, rows = paper_vs_actual(s, seeded)
        diffs = {r["ticker"]: r["diff_eur"] for r in rows if abs(r["diff_eur"]) > 1}
        assert set(diffs) <= {"VUSA", "CASH_USD"} and diffs["VUSA"] > 0
        # idempotent resubmit returns the same order; a second order for the same instrument/day is refused
        assert submit_decision(s, d, settings=seeded, today=EARLY).id == row.id
        d2 = _buy_decision(s, vusa, EARLY, 0.02, ref.close)
        row2 = submit_decision(s, d2, settings=seeded, today=EARLY)
        assert row2.status == "rejected" and "one order per instrument per day" in row2.error


def test_paper_sell_and_limit_orders(seeded):
    with session_scope(seeded) as s:
        run_pipeline(s, seeded, EARLY)
        tsla = _inst(s, "TSLA")
        sell = Decision(
            date=EARLY,
            instrument_id=tsla.id,
            action="SELL",
            size_pct=0.03,
            rules_json={"reference_price": 350.0},
            reasoning_md="# SELL",
            created_at=dt.datetime.utcnow(),
        )
        s.add(sell)
        s.commit()
        row = submit_decision(s, sell, settings=seeded, today=EARLY)
        assert row.side == "SELL" and row.quantity == pytest.approx(10.26)
        fills = settle_paper(s, seeded)
        f = next(x for x in fills if x.order_id == row.id)
        nxt = s.exec(
            select(Price)
            .where(Price.instrument_id == tsla.id, Price.date > EARLY)
            .order_by(Price.date)
        ).first()
        assert f.price == pytest.approx(
            nxt.open * (1 - 10 / 1e4)
        )  # US large cap: 10 bps, sell side
        assert (
            s.exec(
                select(Position).where(
                    Position.broker == "paper",
                    Position.instrument_id == tsla.id,
                    Position.closed_at.is_(None),
                )
            ).first()
            is None
        )
        # LIMIT: unreachable limit expires at DAY; reachable one fills at min(open, limit)
        from desk.broker.paper import PaperBroker

        broker = PaperBroker(s, seeded)
        vusa = _inst(s, "VUSA")
        nxt_v = s.exec(
            select(Price)
            .where(Price.instrument_id == vusa.id, Price.date > EARLY)
            .order_by(Price.date)
        ).first()
        far = Order(
            decision_id=sell.id,
            instrument_id=vusa.id,
            side="BUY",
            notional=100,
            order_type="LIMIT",
            limit_price=nxt_v.low * 0.5,
            client_ref=f"desk-{sell.id}-2026-09-01-limitfar",
        )
        near = Order(
            decision_id=sell.id,
            instrument_id=vusa.id,
            side="BUY",
            notional=100,
            order_type="LIMIT",
            limit_price=nxt_v.high * 1.5,
            client_ref=f"desk-{sell.id}-2026-09-01-limitnear",
        )
        broker.submit(far)
        broker.submit(near)
        broker.settle()
        rows = {r.client_ref: r for r in s.exec(select(OrderRow)).all()}
        assert (
            rows[far.client_ref].status == "cancelled"
            and "DAY expired" in rows[far.client_ref].error
        )
        assert rows[near.client_ref].status == "filled"
        fill = s.exec(select(FillRow).where(FillRow.order_id == rows[near.client_ref].id)).one()
        assert fill.price == pytest.approx(min(nxt_v.open, near.limit_price))


def test_kill_switch_and_live_guards(seeded, monkeypatch):
    with session_scope(seeded) as s:
        run_pipeline(s, seeded, EARLY)
        vusa = _inst(s, "VUSA")
        engage_kill_switch(seeded)
        d = _buy_decision(s, vusa, EARLY, 0.05, 126.0)
        row = submit_decision(s, d, settings=seeded, today=EARLY)
        assert row.status == "rejected" and "kill switch" in row.error
        assert settle_paper(s, seeded) == []
        with pytest.raises(GuardError):
            release_kill_switch("yes", seeded)
        release_kill_switch("CONFIRM", seeded)
        assert not seeded.kill_file.exists()
        # live adapter without DESK_LIVE
        monkeypatch.setattr(seeded, "broker", "ibkr")
        d2 = _buy_decision(s, vusa, EARLY, 0.05, 126.0)
        row2 = submit_decision(s, d2, settings=seeded, today=EARLY)
        assert row2.status == "rejected" and "DESK_LIVE" in row2.error
        # live enabled but over the per-order cap: rejected and logged as a mandatory rule
        monkeypatch.setattr(seeded, "live", True)
        d3 = _buy_decision(s, vusa, EARLY, 0.05, 126.0)  # 5% of ~€99k = €4.9k > €1,000 cap
        row3 = submit_decision(s, d3, settings=seeded, today=EARLY)
        assert row3.status == "rejected" and "max_order_notional_eur" in row3.error
        assert s.exec(
            select(RuleFired).where(
                RuleFired.rule == "live_notional_cap", RuleFired.severity == "mandatory"
            )
        ).first()
        # within caps the stub itself refuses: nothing can reach a live broker before phase 5
        d4 = _buy_decision(s, vusa, EARLY, 0.005, 126.0)
        row4 = submit_decision(s, d4, settings=seeded, today=EARLY)
        assert row4.status == "rejected" and "phase 5" in row4.error


def test_approve_routes_order(seeded):
    with session_scope(seeded) as s:
        run_pipeline(s, seeded, EARLY)
        vusa = _inst(s, "VUSA")
        d = _buy_decision(s, vusa, EARLY, 0.03, 126.0)
        respond(s, d, "approved", "go", seeded)
        assert d.user_status == "approved"
        order = s.exec(select(OrderRow).where(OrderRow.decision_id == d.id)).one()
        assert order.status in ("submitted", "filled") and order.order_date == dt.date.today()
        with pytest.raises(ValueError):
            respond(s, d, "bogus")


# ------------------------------------------------------------------------------------------ 8c screener
def _fake_constituents(source):
    if source == "sp500":
        return [
            {
                "ticker": "AAPL",
                "name": "Apple",
                "sector": "Information Technology",
                "exchange": "NASDAQ",
                "region": "USA",
                "currency": "USD",
                "source_symbol": "AAPL",
            },
            {
                "ticker": "XOM",
                "name": "Exxon",
                "sector": "Energy",
                "exchange": "NYSE",
                "region": "USA",
                "currency": "USD",
                "source_symbol": "XOM",
            },
            {
                "ticker": "JPM",
                "name": "JPMorgan",
                "sector": "Financials",
                "exchange": "NYSE",
                "region": "USA",
                "currency": "USD",
                "source_symbol": "JPM",
            },
        ]
    return [
        {
            "ticker": "SAP",
            "name": "SAP",
            "sector": "Information Technology",
            "exchange": "Europe",
            "region": "Euro area",
            "currency": "EUR",
            "source_symbol": "SAP.DE",
        }
    ]


def _clone_prices(s, src: Instrument, dst: Instrument, factor: float):
    for p in s.exec(select(Price).where(Price.instrument_id == src.id)).all():
        s.add(
            Price(
                instrument_id=dst.id,
                date=p.date,
                open=p.open * factor,
                high=p.high * factor,
                low=p.low * factor,
                close=p.close * factor,
                volume=p.volume,
                source=p.source,
                fetched_at=p.fetched_at,
            )
        )


def test_screener_end_to_end(seeded):
    with session_scope(seeded) as s:
        res = refresh_constituents(s, seeded, fetch=_fake_constituents)
        assert (
            res["sp500"]["added"] == 3
            and res["stoxx600"]["added"] == 1
            and res["safra_focus_list"]["members"] == 20
        )
        members = {
            i.ticker: i
            for i in s.exec(select(Instrument).where(Instrument.screener_member.is_not(None))).all()
        }
        assert members["AAPL"].tradable and not members["SAP"].tradable
        assert (
            _inst(s, "NVDA").screener_member is None
        )  # focus-list name that is a core instrument stays core
        assert (
            members["XOM"].sector == "Energy"
            and members["XOM"].theme == "energy"
            and members["JPM"].sector == "Banks"
        )
        # a name that drops out is flagged, not deleted
        res2 = refresh_constituents(
            s,
            seeded,
            fetch=lambda src: [r for r in _fake_constituents(src) if r["ticker"] != "JPM"],
            only=["sp500"],
        )
        assert (
            res2["sp500"]["dropped"] == 1 and s.get(Instrument, members["JPM"].id).screener_dropped
        )
        # give the survivors prices (cloned from TSLA/NVDA) and fundamentals
        tsla, nvda = _inst(s, "TSLA"), _inst(s, "NVDA")
        _clone_prices(s, nvda, members["AAPL"], 0.5)
        _clone_prices(s, tsla, members["XOM"], 0.3)
        good = {
            "trailingPE": 18.0,
            "forwardPE": 15.0,
            "pegRatio": 0.9,
            "freeCashflow": 5e9,
            "totalDebt": 1e9,
            "totalCash": 5e8,
            "ebitda": 4e9,
            "revenueGrowth": 0.08,
            "earningsGrowth": 0.1,
            "numberOfAnalystOpinions": 30,
            "marketCap": 3e12,
        }
        trap = {
            "trailingPE": 7.0,
            "forwardPE": 9.0,
            "pegRatio": 0.6,
            "freeCashflow": 2e9,
            "totalDebt": 4e9,
            "totalCash": 1e9,
            "ebitda": 3e9,
            "revenueGrowth": -0.03,
            "earningsGrowth": -0.109,
            "numberOfAnalystOpinions": 25,
            "marketCap": 4e11,
        }
        for t, f in (("AAPL", good), ("XOM", trap)):
            for k, v in f.items():
                s.add(
                    Fundamental(
                        instrument_id=members[t].id, date=TODAY, field=k, value=v, source="test"
                    )
                )
        s.commit()
        out = run_screener(s, seeded, TODAY)
        assert out["scored"] >= 3 and out["written"] >= 3
        rows = {
            s.get(Instrument, r.instrument_id).ticker: r
            for r in s.exec(select(ScreenerRow).where(ScreenerRow.date == TODAY)).all()
        }
        assert rows["AAPL"].gates_json["passed"] is True
        assert (
            rows["XOM"].gates_json["passed"] is False
            and rows["XOM"].gates_json["value_trap"] is True
        )
        assert rows["XOM"].factors_json["factors"]["valuation"]["value"] <= 2.0
        assert (
            rows["XOM"].factors_json["safra"]["stance"] == "least_preferred"
        )  # sector view carried through
        assert (
            rows["AAPL"].factors_json["sentiment"]["level"] == "sector"
        )  # no ticker sentiment: sector proxy, flagged
        # anti-churn: one day in the top 15 is not enough to propose
        assert days_in_top(s, members["AAPL"].id, TODAY, 15) == 1
        page = page_rows(s, seeded, TODAY)
        aapl = next(it for it in page["candidates"] if it["inst"].ticker == "AAPL")
        assert aapl["streak"] == 1 and not aapl["can_propose"]
        for d in (dt.date(2026, 9, 2), dt.date(2026, 9, 3)):
            s.add(
                ScreenerRow(
                    date=d,
                    instrument_id=members["AAPL"].id,
                    rank=1,
                    total=80,
                    factors_json=rows["AAPL"].factors_json,
                    gates_json=rows["AAPL"].gates_json,
                )
            )
        s.commit()
        assert days_in_top(s, members["AAPL"].id, TODAY, 15) == 3
        # propose BUY: a normal decision with drafted kill conditions
        d = propose_buy(s, members["AAPL"].id, seeded, TODAY)
        assert (
            d.action == "BUY"
            and d.rules_json["source"] == "screener"
            and d.rules_json["kill_json"]["kills"][0]["severity"] == "mandatory"
        )
        assert (
            "avg_cost('AAPL')" in d.rules_json["kill_predicate"]
            and "Proposed from the screener" in d.reasoning_md
        )
        assert propose_buy(s, members["AAPL"].id, seeded, TODAY).id == d.id  # idempotent per day
        page = page_rows(s, seeded, TODAY)
        aapl = next(it for it in page["candidates"] if it["inst"].ticker == "AAPL")
        assert aapl["proposed"] is not None and not aapl["can_propose"]
        assert ScreenerConfig.load(seeded).top_n == 15


def test_screener_page_and_kill_switch_routes(seeded):
    import base64
    import html

    from fastapi.testclient import TestClient

    from desk.web.app import create_app

    auth = {"Authorization": "Basic " + base64.b64encode(b"u:p").decode()}
    with session_scope(seeded) as s:
        refresh_constituents(s, seeded, fetch=_fake_constituents)
        run_screener(s, seeded, TODAY)
    with TestClient(create_app(seeded)) as c:
        body = html.unescape(c.get("/screener", headers=auth).text)
        assert (
            "Candidates" in body
            and "Avoid" in body
            and "gates" in body
            and ">Propose BUY</button>" not in body
        )  # streak 1 day
        r = c.post("/kill-switch/engage", headers=auth, follow_redirects=False)
        assert r.status_code == 303 and seeded.kill_file.exists()
        assert "KILL SWITCH ACTIVE" in c.get("/decisions", headers=auth).text
        r = c.post(
            "/kill-switch/release", data={"confirm": "nope"}, headers=auth, follow_redirects=False
        )
        assert seeded.kill_file.exists()
        c.post(
            "/kill-switch/release",
            data={"confirm": "CONFIRM"},
            headers=auth,
            follow_redirects=False,
        )
        assert not seeded.kill_file.exists()
