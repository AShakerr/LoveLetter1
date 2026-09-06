"""Disclosed Flow tracker (docs/BRIEF.md 8d): storage of disclosed trades, the classification rules that need
history (routine trades), the daily per-instrument signals, and the read model for the Flow page and badges.

Nothing here creates a decision. Signals feed the Crowd factor (+1 in the 30-70 band, step 7 of the build
order) and the Screener badges; sales on held names become REVIEW flags, never mandatory exits."""

from __future__ import annotations

import datetime as dt
import logging
import math
from collections import defaultdict
from typing import Any

from sqlmodel import Session, select

from desk.config import Settings, get_settings
from desk.models import DisclosedTrade, FlowSignal, Instrument, InstrumentKind, Position
from desk.sources.base import utcnow

log = logging.getLogger(__name__)

CLUSTER_WINDOW_DAYS = 30
CLUSTER_MIN_FILERS = 3
NET_FLOW_WINDOW_DAYS = 90
NET_FLOW_HISTORY_YEARS = 3
CONGRESS_WINDOW_DAYS = 60
CONGRESS_DECAY_DAYS = 90
ROUTINE_YEARS = 3
ROUTINE_MIN_YEARS = 2
SIGNAL_LABELS = {
    "insider_cluster_buy": "insider cluster buy",
    "insider_net_flow": "insider net flow",
    "insider_sale_cluster": "disclosed selling",
    "congress_relevant_buy": "committee-relevant congressional buy",
    "congress_cluster": "congressional cluster (watch only)",
}


# ------------------------------------------------------------------------------------------------ storage
def is_routine_trade(
    session: Session, filer_name: str, issuer_ticker: str, trade_date: dt.date
) -> bool:
    """Cohen-Malloy-Pomorski rule: the same filer traded the same issuer in the same calendar month in at
    least 2 of the prior 3 years. Needs stored history; empty history means not routine (and says so)."""
    years = {
        r.trade_date.year
        for r in session.exec(
            select(DisclosedTrade).where(
                DisclosedTrade.filer_name == filer_name,
                DisclosedTrade.issuer_ticker == issuer_ticker,
                DisclosedTrade.trade_date < dt.date(trade_date.year, 1, 1),
                DisclosedTrade.trade_date >= dt.date(trade_date.year - ROUTINE_YEARS, 1, 1),
            )
        ).all()
        if r.trade_date.month == trade_date.month
    }
    return len(years) >= ROUTINE_MIN_YEARS


def store_trades(session: Session, rows: list[dict[str, Any]]) -> dict[str, int]:
    """Insert parsed trade rows (form4.trades_from_raw shape or the Congress shape), skipping duplicates.
    instrument_id is resolved by ticker; unknown issuers are kept with instrument_id null."""
    counts = {"inserted": 0, "duplicate": 0, "unknown_issuer": 0}
    by_ticker = {i.ticker: i.id for i in session.exec(select(Instrument)).all()}
    for r in rows:
        exists = session.exec(
            select(DisclosedTrade).where(
                DisclosedTrade.source == r["source"],
                DisclosedTrade.raw_url == r["raw_url"],
                DisclosedTrade.filer_name == r["filer_name"],
                DisclosedTrade.trade_date == r["trade_date"],
                DisclosedTrade.transaction_code == r.get("transaction_code"),
                DisclosedTrade.quantity == r.get("quantity"),
                DisclosedTrade.price == r.get("price"),
            )
        ).first()
        if exists is not None:
            counts["duplicate"] += 1
            continue
        inst_id = by_ticker.get(r["issuer_ticker"])
        if inst_id is None:
            counts["unknown_issuer"] += 1
        session.add(
            DisclosedTrade(
                source=r["source"],
                filer_name=r["filer_name"],
                filer_role=r.get("filer_role"),
                issuer_ticker=r["issuer_ticker"],
                instrument_id=inst_id,
                trade_date=r["trade_date"],
                filed_date=r["filed_date"],
                lag_days=r.get("lag_days", (r["filed_date"] - r["trade_date"]).days),
                side=r["side"],
                asset_type=r.get("asset_type", "stock"),
                transaction_code=r.get("transaction_code"),
                quantity=r.get("quantity"),
                amount_low=r.get("amount_low"),
                amount_high=r.get("amount_high"),
                price=r.get("price"),
                is_open_market=bool(r.get("is_open_market")),
                is_10b5_1=bool(r.get("is_10b5_1")),
                is_routine=is_routine_trade(
                    session, r["filer_name"], r["issuer_ticker"], r["trade_date"]
                ),
                committee_relevant=bool(r.get("committee_relevant")),
                raw_url=r["raw_url"],
                fetched_at=r.get("fetched_at") or utcnow(),
            )
        )
        counts["inserted"] += 1
    session.commit()
    return counts


# ------------------------------------------------------------------------------------------------ signals
def _informative_insider(t: DisclosedTrade) -> bool:
    return (
        t.source == "form4"
        and t.is_open_market
        and not t.is_10b5_1
        and not t.is_routine
        and t.asset_type == "stock"
    )


def _net_flow(trades: list[DisclosedTrade], end: dt.date, window: int) -> float:
    start = end - dt.timedelta(days=window)
    net = 0.0
    for t in trades:
        if start < t.trade_date <= end and _informative_insider(t):
            v = t.value or 0.0
            net += v if t.side == "buy" else -v
    return net


def net_flow_percentile(
    trades: list[DisclosedTrade], today: dt.date, window: int = NET_FLOW_WINDOW_DAYS
) -> tuple[float | None, dict[str, Any]]:
    """Today's trailing net flow as a percentile of the issuer's own history of rolling windows (monthly
    steps over 3 years). None until at least 8 history points exist."""
    current = _net_flow(trades, today, window)
    hist: list[float] = []
    d = today - dt.timedelta(days=30)
    floor = today - dt.timedelta(days=365 * NET_FLOW_HISTORY_YEARS)
    while d >= floor:
        hist.append(_net_flow(trades, d, window))
        d -= dt.timedelta(days=30)
    earliest = min((t.trade_date for t in trades), default=None)
    if earliest is None or (today - earliest).days < window * 2 or len(hist) < 8:
        return None, {
            "net_90d": round(current, 2),
            "note": "insufficient history for a 3-year percentile; shown, not scored",
        }
    below = sum(1 for h in hist if h < current)
    return below / len(hist) * 100, {"net_90d": round(current, 2), "history_points": len(hist)}


def compute_signals(
    session: Session, today: dt.date | None = None, instrument_ids: set[int] | None = None
) -> list[FlowSignal]:
    """The 8d signals for every instrument with disclosed trades, replacing today's rows."""
    today = today or dt.date.today()
    since = today - dt.timedelta(days=365 * NET_FLOW_HISTORY_YEARS)
    trades = session.exec(
        select(DisclosedTrade).where(
            DisclosedTrade.instrument_id.is_not(None), DisclosedTrade.trade_date >= since
        )
    ).all()
    by_inst: dict[int, list[DisclosedTrade]] = defaultdict(list)
    for t in trades:
        if instrument_ids is None or t.instrument_id in instrument_ids:
            by_inst[t.instrument_id].append(t)
    for old in session.exec(select(FlowSignal).where(FlowSignal.date == today)).all():
        session.delete(old)
    session.commit()
    out: list[FlowSignal] = []
    for inst_id, rows in by_inst.items():
        w30 = today - dt.timedelta(days=CLUSTER_WINDOW_DAYS)
        buys = [
            t for t in rows if _informative_insider(t) and t.side == "buy" and t.trade_date > w30
        ]
        sells = [
            t for t in rows if _informative_insider(t) and t.side == "sell" and t.trade_date > w30
        ]
        buyers = {t.filer_name for t in buys}
        if len(buyers) >= CLUSTER_MIN_FILERS:
            total = sum(t.value or 0.0 for t in buys)
            out.append(
                FlowSignal(
                    date=today,
                    instrument_id=inst_id,
                    signal="insider_cluster_buy",
                    strength=round(len(buyers) * math.log(max(total, 1.0)), 3),
                    scored=True,
                    detail_json={
                        "filers": sorted(buyers),
                        "n": len(buyers),
                        "total_value": round(total, 2),
                        "last_trade": max(t.trade_date for t in buys).isoformat(),
                        "window_days": CLUSTER_WINDOW_DAYS,
                    },
                )
            )
        sellers = {t.filer_name for t in sells}
        if len(sellers) >= CLUSTER_MIN_FILERS:
            total = sum(t.value or 0.0 for t in sells)
            out.append(
                FlowSignal(
                    date=today,
                    instrument_id=inst_id,
                    signal="insider_sale_cluster",
                    strength=round(len(sellers) * math.log(max(total, 1.0)), 3),
                    scored=False,
                    detail_json={
                        "filers": sorted(sellers),
                        "n": len(sellers),
                        "total_value": round(total, 2),
                        "last_trade": max(t.trade_date for t in sells).isoformat(),
                        "note": "a REVIEW flag on a held name, never a mandatory exit",
                    },
                )
            )
        p, info = net_flow_percentile(rows, today)
        if info.get("net_90d"):
            out.append(
                FlowSignal(
                    date=today,
                    instrument_id=inst_id,
                    signal="insider_net_flow",
                    strength=None if p is None else round(p, 1),
                    scored=p is not None and info["net_90d"] > 0,
                    detail_json={
                        **info,
                        "percentile": None if p is None else round(p, 1),
                        "note": info.get("note")
                        or ("only the buy side is scored" if info["net_90d"] <= 0 else None),
                    },
                )
            )
        # congressional signals (steps 4-6 of the build order) attach here once those sources exist
    for s in out:
        session.add(s)
    session.commit()
    return out


def active_signals(
    session: Session, instrument_id: int, today: dt.date | None = None
) -> list[FlowSignal]:
    """The most recent signal set for an instrument on or before `today`."""
    today = today or dt.date.today()
    latest = session.exec(
        select(FlowSignal)
        .where(FlowSignal.instrument_id == instrument_id, FlowSignal.date <= today)
        .order_by(FlowSignal.date.desc())
    ).first()
    if latest is None:
        return []
    return list(
        session.exec(
            select(FlowSignal).where(
                FlowSignal.instrument_id == instrument_id, FlowSignal.date == latest.date
            )
        ).all()
    )


def flow_badge(session: Session, instrument_id: int, today: dt.date | None = None) -> str | None:
    """One line for a screener row or decision page: 'insider cluster buy: 3 insiders (A, B, C), last 2026-09-03'."""
    parts = []
    for s in active_signals(session, instrument_id, today):
        d = s.detail_json or {}
        if s.signal in ("insider_cluster_buy", "insider_sale_cluster"):
            parts.append(
                f"{SIGNAL_LABELS[s.signal]}: {d.get('n')} filers ({', '.join(d.get('filers', [])[:4])}), "
                f"last {d.get('last_trade')}"
            )
        elif s.signal == "insider_net_flow" and s.strength is not None and s.scored:
            parts.append(f"insider net buying P{s.strength:.0f} of own 3y range")
        elif s.signal == "congress_relevant_buy":
            parts.append(f"{SIGNAL_LABELS[s.signal]}: {d.get('member')} ({d.get('lag_days')}d lag)")
    return "; ".join(parts) or None


# ------------------------------------------------------------------------------------------------ job
def flow_tickers(session: Session) -> list[str]:
    """Screener universe (tradable members) plus held US single names."""
    members = session.exec(
        select(Instrument).where(
            Instrument.screener_member.is_not(None),
            Instrument.screener_dropped.is_(False),
            Instrument.tradable.is_(True),
        )
    ).all()
    held_ids = {
        p.instrument_id
        for p in session.exec(
            select(Position).where(
                Position.confirmed_by_user.is_(True), Position.closed_at.is_(None)
            )
        ).all()
    }
    held = [
        i
        for i in session.exec(select(Instrument).where(Instrument.id.in_(held_ids))).all()
        if i.kind == InstrumentKind.stock and (i.region == "USA" or i.exchange in ("NYSE/NASDAQ",))
    ]
    out: list[str] = []
    for i in held + members:
        if i.ticker not in out and "." not in (i.source_symbol or i.ticker):
            out.append(i.ticker)
    return out


def run_flow_daily(
    settings: Settings | None = None, today: dt.date | None = None, fetcher=None
) -> dict[str, Any]:
    """Fetch the last two business days of Form 4 filings for the universe, store, compute signals."""
    from desk.db import init_db, session_scope
    from desk.models import FetchRun
    from desk.sources.form4 import Form4Fetcher

    settings = settings or get_settings()
    today = today or dt.date.today()
    init_db(settings)
    started = utcnow()
    with session_scope(settings) as session:
        f = fetcher or Form4Fetcher(flow_tickers(session), settings=settings, today=today)
        outcome = f.run()
        counts: dict[str, int] = {}
        if outcome.status == "ok" and outcome.raw is not None:
            counts = store_trades(session, f.trades(outcome.raw))
        signals = compute_signals(session, today)
        session.add(
            FetchRun(
                source="form4",
                started_at=started,
                finished_at=utcnow(),
                status=outcome.status,
                rows=counts.get("inserted", 0),
                error=outcome.error,
            )
        )
        session.commit()
        return {
            "source": "form4",
            "status": outcome.status,
            "rows": counts.get("inserted", 0),
            "counts": counts,
            "signals": len(signals),
            "error": outcome.error,
        }


# ------------------------------------------------------------------------------------------- read model
def page_data(
    session: Session,
    today: dt.date | None = None,
    source: str | None = None,
    signal: str | None = None,
    days: int = 3,
) -> dict[str, Any]:
    today = today or dt.date.today()
    q = select(DisclosedTrade).where(DisclosedTrade.filed_date >= today - dt.timedelta(days=days))
    if source:
        q = q.where(DisclosedTrade.source == source)
    trades = sorted(session.exec(q).all(), key=lambda t: (t.filed_date, t.trade_date), reverse=True)
    latest_signal_date = session.exec(
        select(FlowSignal.date).order_by(FlowSignal.date.desc()).limit(1)
    ).first()
    signals = (
        session.exec(select(FlowSignal).where(FlowSignal.date == latest_signal_date)).all()
        if latest_signal_date
        else []
    )
    sig_by_inst: dict[int, list[FlowSignal]] = defaultdict(list)
    for s in signals:
        sig_by_inst[s.instrument_id].append(s)
    if signal:
        keep = {i for i, ss in sig_by_inst.items() if any(s.signal == signal for s in ss)}
        trades = [t for t in trades if t.instrument_id in keep]
    inst = {i.id: i for i in session.exec(select(Instrument)).all()}
    rows = []
    for t in trades:
        rows.append(
            {
                "t": t,
                "inst": inst.get(t.instrument_id),
                "value": t.value,
                "signals": sig_by_inst.get(t.instrument_id, []),
                "scored": _informative_insider(t) and t.side == "buy",
                "why_zero": (
                    None
                    if _informative_insider(t) and t.side == "buy"
                    else "sale: shown, not scored"
                    if _informative_insider(t)
                    else f"not open-market (code {t.transaction_code})"
                    if not t.is_open_market
                    else "10b5-1 plan"
                    if t.is_10b5_1
                    else "routine calendar trade"
                ),
            }
        )
    counts = {
        "filings": len({t.raw_url for t in trades}),
        "trades": len(trades),
        "open_market_buys": sum(1 for t in trades if _informative_insider(t) and t.side == "buy"),
        "scored_signals": sum(1 for s in signals if s.scored),
    }
    return {
        "date": today,
        "days": days,
        "rows": rows,
        "signals": sorted(signals, key=lambda s: -(s.strength or 0)),
        "signal_labels": SIGNAL_LABELS,
        "inst": inst,
        "counts": counts,
        "watch": [],  # people whose trades were followed by outperformance in this system's own log; empty until step 8
        "source": source,
        "signal": signal,
    }
