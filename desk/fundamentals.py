"""Weekly fundamentals (docs/BRIEF.md 7c): yfinance Ticker.info for the universe, Alpha Vantage OVERVIEW as the
fallback, every field stored in `fundamentals(instrument_id, date, field, value, source)`."""

from __future__ import annotations

import datetime as dt
import logging
import statistics
from collections.abc import Callable
from typing import Any

from sqlmodel import Session, select

from desk.config import Settings, get_settings
from desk.models import Fundamental, Instrument, InstrumentKind
from desk.sources.alphavantage import CallBudget
from desk.sources.base import http_get_json

log = logging.getLogger(__name__)

FIELDS = [
    "trailingPE",
    "forwardPE",
    "pegRatio",
    "priceToBook",
    "enterpriseToEbitda",
    "freeCashflow",
    "totalDebt",
    "totalCash",
    "ebitda",
    "revenueGrowth",
    "earningsGrowth",
    "targetMeanPrice",
    "numberOfAnalystOpinions",
    "recommendationMean",
    "marketCap",
    "trailingEps",
    "forwardEps",
]
AV_MAP = {
    "PERatio": "trailingPE",
    "ForwardPE": "forwardPE",
    "PEGRatio": "pegRatio",
    "PriceToBookRatio": "priceToBook",
    "EVToEBITDA": "enterpriseToEbitda",
    "EBITDA": "ebitda",
    "QuarterlyRevenueGrowthYOY": "revenueGrowth",
    "QuarterlyEarningsGrowthYOY": "earningsGrowth",
    "AnalystTargetPrice": "targetMeanPrice",
    "MarketCapitalization": "marketCap",
    "EPS": "trailingEps",
}
FINANCIAL_SECTORS = {"Financials", "Financial Services", "Banks", "Insurance"}


def _num(v: Any) -> float | None:
    if v is None or v in ("None", "", "-", "N/A"):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def fetch_yfinance_info(symbol: str) -> dict[str, Any]:
    import yfinance as yf

    info = yf.Ticker(symbol).info or {}
    out = {f: _num(info.get(f)) for f in FIELDS}
    out["_sector"] = info.get("sector")
    return out


def fetch_alphavantage_overview(symbol: str, api_key: str, budget: CallBudget) -> dict[str, Any]:
    if budget.remaining() <= 0:
        raise RuntimeError("alphavantage daily budget exhausted")
    budget.consume()
    payload = http_get_json(
        "https://www.alphavantage.co/query",
        params={"function": "OVERVIEW", "symbol": symbol, "apikey": api_key},
    )
    out = {v: _num(payload.get(k)) for k, v in AV_MAP.items()}
    out["_sector"] = payload.get("Sector")
    return out


def fetch_yfinance_earnings(symbol: str, limit: int = 8) -> list[dict[str, Any]]:
    """[{date, eps_estimate, reported_eps}] from yfinance Ticker.earnings_dates (past and upcoming)."""
    import yfinance as yf

    df = yf.Ticker(symbol).get_earnings_dates(limit=limit)
    out: list[dict[str, Any]] = []
    if df is None or len(df) == 0:
        return out
    for idx, row in df.iterrows():
        d = idx.date() if hasattr(idx, "date") else dt.date.fromisoformat(str(idx)[:10])
        est = row.get("EPS Estimate") if hasattr(row, "get") else None
        rep = row.get("Reported EPS") if hasattr(row, "get") else None
        out.append({"date": d.isoformat(), "eps_estimate": _num(est), "reported_eps": _num(rep)})
    return out


def store_earnings(session: Session, instrument: Instrument, rows: list[dict[str, Any]]) -> int:
    from desk.events import add_earnings_event

    n = 0
    for r in rows:
        add_earnings_event(
            session,
            instrument,
            dt.date.fromisoformat(r["date"]),
            r.get("eps_estimate"),
            r.get("reported_eps"),
        )
        n += 1
    return n


def store_fundamentals(
    session: Session, instrument: Instrument, on: dt.date, values: dict[str, Any], source: str
) -> int:
    n = 0
    for field, value in values.items():
        if field.startswith("_"):
            continue
        row = session.exec(
            select(Fundamental).where(
                Fundamental.instrument_id == instrument.id,
                Fundamental.date == on,
                Fundamental.field == field,
                Fundamental.source == source,
            )
        ).first()
        if row is None:
            session.add(
                Fundamental(
                    instrument_id=instrument.id, date=on, field=field, value=value, source=source
                )
            )
            n += 1
        elif row.value != value:
            row.value = value
            session.add(row)
    sector = values.get("_sector")
    if sector and not instrument.sector:
        instrument.sector = sector
        session.add(instrument)
    session.commit()
    return n


def latest_fundamentals(
    session: Session, instrument_id: int
) -> dict[str, tuple[float | None, dt.date, str]]:
    """field -> (value, as_of, source), newest row per field."""
    rows = session.exec(
        select(Fundamental)
        .where(Fundamental.instrument_id == instrument_id)
        .order_by(Fundamental.date.desc(), Fundamental.id.desc())
    ).all()
    out: dict[str, tuple[float | None, dt.date, str]] = {}
    for r in rows:
        if r.field not in out:
            out[r.field] = (r.value, r.date, r.source)
    return out


def latest_field_map(session: Session, field: str) -> dict[int, float]:
    rows = session.exec(
        select(Fundamental)
        .where(Fundamental.field == field, Fundamental.value.is_not(None))
        .order_by(Fundamental.date.desc())
    ).all()
    out: dict[int, float] = {}
    for r in rows:
        out.setdefault(r.instrument_id, r.value)
    return out


def history_median(
    session: Session, instrument_id: int, field: str, years: int = 5, today: dt.date | None = None
) -> tuple[float | None, int]:
    """Median of the weekly history of a field over `years`, and the number of distinct weeks it rests on."""
    today = today or dt.date.today()
    rows = session.exec(
        select(Fundamental).where(
            Fundamental.instrument_id == instrument_id,
            Fundamental.field == field,
            Fundamental.value.is_not(None),
            Fundamental.date >= today - dt.timedelta(days=365 * years),
        )
    ).all()
    by_week: dict[tuple[int, int], float] = {}
    for r in rows:
        by_week[r.date.isocalendar()[:2]] = r.value
    if not by_week:
        return None, 0
    return statistics.median(by_week.values()), len(by_week)


def sector_stats(session: Session, field: str = "forwardPE") -> dict[str, dict[str, float]]:
    """Per sector across every instrument with a stored value: median, standard deviation, count."""
    latest = latest_field_map(session, field)
    by_sector: dict[str, list[float]] = {}
    for inst in session.exec(
        select(Instrument).where(Instrument.kind == InstrumentKind.stock)
    ).all():
        v = latest.get(inst.id)
        if v is None or v <= 0 or v > 200 or not inst.sector:
            continue
        by_sector.setdefault(inst.sector, []).append(v)
    out = {}
    for sector, vals in by_sector.items():
        out[sector] = {
            "median": statistics.median(vals),
            "std": statistics.pstdev(vals) if len(vals) > 1 else 0.0,
            "n": len(vals),
        }
    return out


def run_weekly(
    session: Session,
    settings: Settings | None = None,
    *,
    instruments: list[Instrument] | None = None,
    fetch: Callable[[str], dict[str, Any]] = fetch_yfinance_info,
    av_fallback: bool = True,
    on: dt.date | None = None,
    fetch_earnings: Callable[[str], list[dict[str, Any]]] | None = fetch_yfinance_earnings,
    earnings_for: list[Instrument] | None = None,
) -> dict[str, Any]:
    """Refresh fundamentals for stocks and ETFs. Slow on purpose: weekly, one symbol at a time."""
    settings = settings or get_settings()
    on = on or dt.date.today()
    if instruments is None:
        instruments = [
            i
            for i in session.exec(select(Instrument)).all()
            if i.kind in (InstrumentKind.stock, InstrumentKind.etf)
            and (i.tradable or i.screener_member)
        ]
    budget = CallBudget(
        settings.cache_dir / "alphavantage.budget.json", settings.alphavantage_daily_budget
    )
    ok, fallback, failed = 0, 0, []
    for inst in instruments:
        symbol = inst.source_symbol or inst.ticker
        values: dict[str, Any] | None = None
        try:
            values = fetch(symbol)
            if not any(v is not None for k, v in values.items() if not k.startswith("_")):
                values = None
        except Exception as exc:  # noqa: BLE001
            log.warning("fundamentals %s: yfinance failed: %s", symbol, exc)
        source = "yfinance"
        if (
            values is None
            and av_fallback
            and settings.alphavantage_api_key
            and inst.kind == InstrumentKind.stock
        ):
            try:
                values = fetch_alphavantage_overview(
                    inst.ticker, settings.alphavantage_api_key, budget
                )
                source = "alphavantage"
                fallback += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("fundamentals %s: alphavantage failed: %s", inst.ticker, exc)
        if values is None:
            failed.append(inst.ticker)
            continue
        store_fundamentals(session, inst, on, values, source)
        ok += 1
    # earnings dates (7b): held and watchlist stocks, past and upcoming, into the events calendar
    earnings = 0
    if fetch_earnings is not None:
        targets = (
            earnings_for
            if earnings_for is not None
            else [
                i for i in instruments if i.kind == InstrumentKind.stock and not i.screener_member
            ]
        )
        for inst in targets:
            try:
                earnings += store_earnings(
                    session, inst, fetch_earnings(inst.source_symbol or inst.ticker)
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("earnings %s: %s", inst.ticker, exc)
    return {
        "date": on.isoformat(),
        "ok": ok,
        "alphavantage_fallback": fallback,
        "failed": failed,
        "earnings_events": earnings,
    }
