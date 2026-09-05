"""Self-scoring (docs/BRIEF.md phase 4): what happened after each decision versus the alternative, including paper
execution costs; hit rate and P&L attribution by rule and by factor; the 8b promotion checklist computed live; the
screener's forward returns versus the S&P 500 / STOXX 600 equal-weight; and the seeded August ideas against prices."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from sqlmodel import Session, select

from desk.broker.base import Costs
from desk.config import Settings, get_settings
from desk.houseviews import all_views
from desk.models import (
    Decision,
    FillRow,
    Instrument,
    OrderRow,
    Price,
    RuleFired,
    Score,
    ScreenerRow,
)
from desk.score import WEIGHTS

WINDOWS = (30, 60, 90)
BENCH_BLEND = ("VUSA", "EXW1")  # 50/50 VUSA and a EURO STOXX 50 ETF (8b)
BENCH_FALLBACK = ("^GSPC", "^STOXX50E")
SCREENER_BENCH = ("^GSPC", "^STOXX")
SCREENER_BENCH_FALLBACK = ("^GSPC", "^STOXX50E")


# ------------------------------------------------------------------------------------------ prices
def price_on_or_before(session: Session, instrument_id: int, on: dt.date) -> Price | None:
    return session.exec(
        select(Price)
        .where(Price.instrument_id == instrument_id, Price.date <= on)
        .order_by(Price.date.desc())
        .limit(1)
    ).first()


def price_on_or_after(session: Session, instrument_id: int, on: dt.date) -> Price | None:
    return session.exec(
        select(Price)
        .where(Price.instrument_id == instrument_id, Price.date >= on)
        .order_by(Price.date)
        .limit(1)
    ).first()


def _ticker_id(session: Session, ticker: str) -> int | None:
    inst = session.exec(select(Instrument).where(Instrument.ticker == ticker)).first()
    return inst.id if inst else None


def instrument_return(
    session: Session, instrument_id: int, start: dt.date, end: dt.date
) -> tuple[float | None, dict[str, Any]]:
    a = price_on_or_before(session, instrument_id, start)
    b = price_on_or_before(session, instrument_id, end)
    if a is None or b is None or not a.close or b.date <= a.date:
        return None, {"note": "no price pair"}
    return b.close / a.close - 1, {
        "from": a.date.isoformat(),
        "to": b.date.isoformat(),
        "from_close": a.close,
        "to_close": b.close,
    }


def blend_return(
    session: Session,
    tickers: tuple[str, ...],
    fallback: tuple[str, ...],
    start: dt.date,
    end: dt.date,
) -> tuple[float | None, list[str]]:
    """Equal-weight return of the named tickers; falls back to indices when the ETFs have no prices."""
    for group in (tickers, fallback):
        rets, used = [], []
        for t in group:
            iid = _ticker_id(session, t)
            if iid is None:
                continue
            r, _ = instrument_return(session, iid, start, end)
            if r is not None:
                rets.append(r)
                used.append(t)
        if rets:
            return sum(rets) / len(rets), used
    return None, []


# --------------------------------------------------------------------------------------- outcomes
@dataclass
class Outcome:
    decision: Decision
    ticker: str
    window: int
    matured: bool
    start: dt.date
    end: dt.date
    instrument_return: float | None
    benchmark_return: float | None
    cost_bps: float
    cost_source: str
    net_excess: float | None
    hit: bool | None
    rule: str
    factor: str
    detail: dict[str, Any] = field(default_factory=dict)


def dominant_factor(score: Score | None) -> str:
    if score is None or not score.inputs_json:
        return "n/a"
    factors = score.inputs_json.get("factors") or {}
    best, best_pts = "n/a", -1.0
    for name, f in factors.items():
        v = f.get("value")
        if v is None:
            continue
        pts = v / 5 * WEIGHTS.get(name, 0)
        if pts > best_pts:
            best, best_pts = name, pts
    return best


def decision_rule(decision: Decision) -> str:
    flags = (decision.rules_json or {}).get("flags") or []
    for f in flags:
        if f.get("severity") == "mandatory":
            return f.get("rule", "mandatory")
    if (decision.rules_json or {}).get("source") == "screener":
        return "screener"
    return {"BUY": "buy_side", "ADD": "buy_side", "HOLD": "hold", "AVOID": "avoid"}.get(
        decision.action, decision.action.lower()
    )


def paper_cost_bps(
    session: Session, decision: Decision, inst: Instrument, costs: Costs
) -> tuple[float, str]:
    """Realised paper cost (slippage + fees) from the fills of this decision, else the configured spread estimate."""
    order = session.exec(
        select(OrderRow).where(OrderRow.decision_id == decision.id, OrderRow.status == "filled")
    ).first()
    if order is not None:
        fill = session.exec(select(FillRow).where(FillRow.order_id == order.id)).first()
        if fill is not None:
            fee_bps = (
                (fill.fees / (fill.quantity * fill.price) * 1e4)
                if fill.quantity and fill.price
                else 0.0
            )
            slip = fill.slippage_bps if fill.slippage_bps is not None else 0.0
            return max(0.0, slip) + fee_bps, "paper fill"
    return costs.spread_for(inst), "costs.yaml estimate"


def outcome_for(
    session: Session,
    decision: Decision,
    inst: Instrument,
    window: int,
    today: dt.date,
    costs: Costs,
    score: Score | None,
) -> Outcome:
    start = decision.date
    target = start + dt.timedelta(days=window)
    matured = target <= today
    end = target if matured else today
    ret, detail = instrument_return(session, inst.id, start, end)
    bench, used = blend_return(session, BENCH_BLEND, BENCH_FALLBACK, start, end)
    cost_bps, cost_src = paper_cost_bps(session, decision, inst, costs)
    cost = cost_bps / 1e4
    net = hit = None
    if ret is not None:
        if decision.action in ("BUY", "ADD"):
            net = ret - cost - (bench or 0.0)  # bought (net of costs) vs the benchmark blend
        elif decision.action in ("SELL", "TRIM"):
            net = -cost - ret  # followed (cash, paid the spread) vs ignored (kept the position)
        elif decision.action == "HOLD":
            net = ret  # held vs sold to cash
        else:  # AVOID
            net = (bench or 0.0) - ret  # avoided vs bought
        hit = net > 0
    return Outcome(
        decision,
        inst.ticker,
        window,
        matured,
        start,
        end,
        ret,
        bench,
        cost_bps,
        cost_src,
        net,
        hit,
        decision_rule(decision),
        dominant_factor(score),
        {**detail, "benchmark": used},
    )


def decision_outcomes(
    session: Session,
    today: dt.date | None = None,
    settings: Settings | None = None,
    windows: tuple[int, ...] = (30, 90),
    matured_only: bool = False,
) -> list[Outcome]:
    settings = settings or get_settings()
    today = today or dt.date.today()
    costs = Costs.load(settings.config_dir / "costs.yaml")
    out: list[Outcome] = []
    for d in session.exec(select(Decision).order_by(Decision.date, Decision.id)).all():
        inst = session.get(Instrument, d.instrument_id)
        score = session.get(Score, d.score_id) if d.score_id else None
        for w in windows:
            o = outcome_for(session, d, inst, w, today, costs, score)
            if matured_only and not o.matured:
                continue
            out.append(o)
    return out


def hit_rate(outcomes: list[Outcome]) -> dict[int, dict[str, Any]]:
    by_w: dict[int, dict[str, Any]] = {}
    for o in outcomes:
        if not o.matured or o.hit is None:
            continue
        b = by_w.setdefault(o.window, {"n": 0, "hits": 0, "sum_excess": 0.0})
        b["n"] += 1
        b["hits"] += int(o.hit)
        b["sum_excess"] += o.net_excess or 0.0
    for b in by_w.values():
        b["rate"] = b["hits"] / b["n"] if b["n"] else None
        b["avg_excess"] = b["sum_excess"] / b["n"] if b["n"] else None
    return by_w


def attribution(
    outcomes: list[Outcome], by: str = "rule", window: int = 30
) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for o in outcomes:
        if o.window != window or not o.matured or o.net_excess is None:
            continue
        key = o.rule if by == "rule" else o.factor
        g = groups.setdefault(key, {"key": key, "n": 0, "hits": 0, "sum_excess": 0.0})
        g["n"] += 1
        g["hits"] += int(bool(o.hit))
        g["sum_excess"] += o.net_excess
    for g in groups.values():
        g["rate"] = g["hits"] / g["n"]
        g["avg_excess"] = g["sum_excess"] / g["n"]
    return sorted(groups.values(), key=lambda g: -g["sum_excess"])


# ------------------------------------------------------------------------------------- promotion
@dataclass
class Criterion:
    name: str
    passed: bool
    value: str
    detail: str


def trading_days_with_decisions(session: Session) -> int:
    return len({d.date for d in session.exec(select(Decision)).all()})


def max_stale_streak(session: Session) -> int:
    dates = sorted(
        {
            r.date
            for r in session.exec(
                select(RuleFired).where(
                    RuleFired.rule == "stale_data", RuleFired.instrument_id.is_(None)
                )
            ).all()
        }
    )
    best = run = 0
    prev = None
    for d in dates:
        run = run + 1 if prev is not None and (d - prev).days <= 1 else 1
        best = max(best, run)
        prev = d
    return best


def promotion_checklist(
    session: Session, today: dt.date | None = None, settings: Settings | None = None
) -> list[Criterion]:
    today = today or dt.date.today()
    outcomes = decision_outcomes(session, today, settings, windows=(30,), matured_only=True)
    days = trading_days_with_decisions(session)
    out = [
        Criterion(
            "At least 60 trading days of paper decisions",
            days >= 60,
            f"{days} days",
            "every criterion below is judged over this window",
        )
    ]
    mand_fired = session.exec(select(RuleFired).where(RuleFired.severity == "mandatory")).all()
    fired = len({(r.date, r.instrument_id, r.rule) for r in mand_fired})
    mand_out = [
        o for o in outcomes if o.decision.action in ("SELL", "TRIM") and o.net_excess is not None
    ]
    agg = sum(o.net_excess for o in mand_out)
    out.append(
        Criterion(
            "Mandatory rules fired ≥ 5 times and following them beat ignoring them at 30 days",
            fired >= 5 and bool(mand_out) and agg > 0,
            f"{fired} fired; {len(mand_out)} matured; aggregate {agg * 100:+.1f}pp",
            "net of paper costs: cash after the exit vs keeping the position",
        )
    )
    buys = [
        o
        for o in outcomes
        if o.decision.action in ("BUY", "ADD")
        and o.instrument_return is not None
        and (o.decision.user_status in ("approved", "executed") or o.cost_source == "paper fill")
    ]
    if buys:
        net = sum(o.instrument_return - o.cost_bps / 1e4 for o in buys) / len(buys)
        bench = sum((o.benchmark_return or 0.0) for o in buys) / len(buys)
        ok = net >= bench
        val = f"{len(buys)} BUYs: {net * 100:+.1f}% net vs blend {bench * 100:+.1f}%"
    else:
        ok, val = False, "no matured executed-equivalent BUY decisions yet"
    out.append(
        Criterion(
            "Executed-equivalent paper BUYs, net of costs, not below the 50/50 VUSA / EURO STOXX 50 blend",
            ok,
            val,
            "same 30-day windows",
        )
    )
    streak = max_stale_streak(session)
    out.append(
        Criterion(
            "No data-staleness incident longer than 3 days",
            streak <= 3,
            f"longest streak {streak} days",
            "consecutive days with a global stale_data flag",
        )
    )
    stale_pending = [
        d
        for d in session.exec(select(Decision).where(Decision.user_status == "pending")).all()
        if (today - d.date).days > 7
    ]
    out.append(
        Criterion(
            "Every decision reviewed (no pending older than 7 days)",
            not stale_pending,
            f"{len(stale_pending)} pending older than 7 days",
            "respond on the Decisions page",
        )
    )
    return out


# ------------------------------------------------------------------------------------- screener
def screener_track(
    session: Session, today: dt.date | None = None, top_n: int = 15
) -> dict[str, Any]:
    """Each day's top N, equal-weight, at 30/60/90 days vs the S&P 500 / STOXX 600 equal-weight."""
    today = today or dt.date.today()
    rows_by_date: dict[dt.date, list[ScreenerRow]] = {}
    for r in session.exec(
        select(ScreenerRow).where(ScreenerRow.rank <= top_n).order_by(ScreenerRow.date)
    ).all():
        rows_by_date.setdefault(r.date, []).append(r)
    table = []
    summary: dict[int, dict[str, Any]] = {
        w: {"n": 0, "hits": 0, "sum_excess": 0.0} for w in WINDOWS
    }
    for d, rows in rows_by_date.items():
        entry: dict[str, Any] = {"date": d, "n": len(rows), "windows": {}}
        for w in WINDOWS:
            end = d + dt.timedelta(days=w)
            matured = end <= today
            rets = [
                r
                for r in (
                    instrument_return(session, row.instrument_id, d, end if matured else today)[0]
                    for row in rows
                )
                if r is not None
            ]
            bench, used = blend_return(
                session, SCREENER_BENCH, SCREENER_BENCH_FALLBACK, d, end if matured else today
            )
            ew = sum(rets) / len(rets) if rets else None
            excess = (ew - bench) if (ew is not None and bench is not None) else None
            entry["windows"][w] = {
                "matured": matured,
                "top_ew": ew,
                "benchmark": bench,
                "excess": excess,
                "priced": len(rets),
                "bench_used": used,
            }
            if matured and excess is not None:
                summary[w]["n"] += 1
                summary[w]["hits"] += int(excess > 0)
                summary[w]["sum_excess"] += excess
        table.append(entry)
    for b in summary.values():
        b["rate"] = b["hits"] / b["n"] if b["n"] else None
        b["avg_excess"] = b["sum_excess"] / b["n"] if b["n"] else None
    return {"rows": table, "summary": summary}


# ---------------------------------------------------------------------------------- seeded ideas
INDEX_FOR_TARGET = {
    "S&P 500": "^GSPC",
    "Nasdaq 100": "^NDX",
    "Euro Stoxx 50": "^STOXX50E",
    "DAX": "^GDAXI",
    "Gold": "GC=F",
    "MSCI EM": "X9I1",
    "MSCI World": "VWCE",
}


def seeded_ideas(session: Session, today: dt.date | None = None) -> list[dict[str, Any]]:
    """Safra's August stock ratings and index/commodity targets against what prices did since the report."""
    today = today or dt.date.today()
    out = []
    seen: set[tuple[str, str]] = set()
    for row in all_views(session):
        v = row.view
        key = (v.scope, v.key)
        if key in seen or row.tactical:
            continue
        if v.scope == "stock":
            iid = _ticker_id(session, v.key)
            if iid is None:
                continue
            ret, detail = instrument_return(session, iid, row.report.date, today)
            seen.add(key)
            out.append(
                {
                    "kind": "stock",
                    "key": v.key,
                    "stance": v.stance,
                    "value": v.value,
                    "report_date": row.report.date,
                    "return": ret,
                    "detail": detail,
                    "hit": (ret > 0)
                    if (ret is not None and v.stance in ("buy", "strong_buy"))
                    else (ret <= 0 if ret is not None and v.stance == "sell" else None),
                }
            )
        elif v.scope in ("index_target", "commodity") and v.value:
            name = next((n for n in INDEX_FOR_TARGET if v.key.startswith(n)), None)
            if name is None:
                continue
            iid = _ticker_id(session, INDEX_FOR_TARGET[name])
            if iid is None:
                continue
            try:
                target = float(str(v.value).replace("'", "").replace(",", "").split("-")[0])
            except ValueError:
                continue
            a = price_on_or_before(session, iid, row.report.date)
            b = price_on_or_before(session, iid, today)
            if a is None or b is None or not a.close:
                continue
            seen.add(key)
            out.append(
                {
                    "kind": v.scope,
                    "key": v.key,
                    "target": target,
                    "at_report": a.close,
                    "latest": b.close,
                    "latest_date": b.date,
                    "return": b.close / a.close - 1,
                    "report_date": row.report.date,
                    "progress": (b.close - a.close) / (target - a.close)
                    if target != a.close
                    else None,
                    "hit": None,
                }
            )
    return out
