"""Crowd factor (docs/BRIEF.md 7b): positioning as a percentile of its 3-year range, surprise, consensus gap, and
the deferral rule. None of this predicts what the crowd will do; it measures what the crowd has already done."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from sqlmodel import Session, select

from desk.events import last_with_actual, surprise_direction, upcoming
from desk.models import Instrument, InstrumentKind, NewsSentiment, Observation

# series -> sign: +1 means a high value = crowd long; -1 means a high value = crowd short (put/call)
COT = {
    "gold": "COT:GOLD",
    "energy": "COT:CRUDE",
    "materials_copper": "COT:COPPER",
    "fx_eur": "COT:EUR",
    "rates": "COT:TNOTE10",
}
EQUITY_SIGNALS = [("CBOE_PUTCALL_TOTAL", -1), ("AAII_BULL_BEAR_SPREAD", +1), ("COT:SP500", +1)]
DISCLAIMER = (
    "Crowd measures what the crowd has already done and what it already expects; it does not predict "
    "what the crowd will do."
)


@dataclass
class CrowdResult:
    value: float | None
    percentile: float | None
    inputs: dict[str, Any] = field(default_factory=dict)
    note: str | None = None


def range_percentile(
    session: Session, series: str, years: int = 3, today: dt.date | None = None
) -> tuple[float | None, dict[str, Any]]:
    """Latest value's position within the min-max range of the last `years`, 0-100."""
    today = today or dt.date.today()
    rows = session.exec(
        select(Observation)
        .where(
            Observation.series == series,
            Observation.date >= today - dt.timedelta(days=365 * years),
            Observation.date <= today,
        )
        .order_by(Observation.date)
    ).all()
    if len(rows) < 8:
        return None, {"series": series, "note": f"{len(rows)} observations; need at least 8"}
    vals = [r.value for r in rows]
    lo, hi = min(vals), max(vals)
    last = rows[-1]
    if hi == lo:
        return 50.0, {
            "series": series,
            "latest": last.value,
            "as_of": last.date.isoformat(),
            "note": "flat range",
        }
    p = (last.value - lo) / (hi - lo) * 100
    return p, {
        "series": series,
        "latest": last.value,
        "as_of": last.date.isoformat(),
        "min": lo,
        "max": hi,
        "n": len(vals),
        "percentile": round(p, 1),
    }


def crowd_long_percentile(
    session: Session, inst: Instrument, today: dt.date | None = None
) -> tuple[float | None, dict[str, Any]]:
    """Positioning most relevant to the instrument's theme, oriented so 100 = crowd maximally long."""
    theme = inst.theme or ""
    inputs: dict[str, Any] = {}
    if theme in COT:
        p, i = range_percentile(session, COT[theme], today=today)
        inputs[COT[theme]] = i
        return p, inputs
    if inst.kind == InstrumentKind.fx:
        p, i = range_percentile(session, COT["fx_eur"], today=today)
        inputs[COT["fx_eur"]] = i
        return p, inputs
    if inst.kind == InstrumentKind.crypto:
        return None, {"note": "no free positioning series for crypto"}
    parts = []
    for series, sign in EQUITY_SIGNALS:
        p, i = range_percentile(session, series, today=today)
        inputs[series] = i
        if p is not None:
            parts.append(p if sign > 0 else 100 - p)
    if not parts:
        return None, inputs
    inputs["composite"] = round(sum(parts) / len(parts), 1)
    return sum(parts) / len(parts), inputs


SENTIMENT_THRESHOLD = 0.15
SECTOR_TOPIC = {
    "Energy": ["energy_transportation", "oil price"],
    "Banks": ["financial_markets"],
    "Communication Services": ["financial_markets"],
    "Utilities": ["economy_macro"],
}
DEFAULT_TOPICS = ["financial_markets", "economy_macro"]


def sentiment_adjust(
    session: Session, inst: Instrument, today: dt.date, days: int = 14
) -> tuple[int, dict[str, Any]]:
    """+1 above +0.15, -1 below -0.15, on the instrument's 14-day Alpha Vantage sentiment; GDELT/topic tone as the
    sector-level fallback (GDELT tone is on a roughly -10..+10 scale and is divided by 10)."""
    since = today - dt.timedelta(days=days)
    own = session.exec(
        select(NewsSentiment).where(
            NewsSentiment.instrument_id == inst.id, NewsSentiment.date >= since
        )
    ).all()
    if own:
        mean = sum(r.score for r in own) / len(own)
        info: dict[str, Any] = {
            "level": "ticker",
            "mean": round(mean, 4),
            "n": len(own),
            "source": own[-1].source,
        }
    else:
        topics = SECTOR_TOPIC.get(inst.sector or "", []) + DEFAULT_TOPICS
        rows = []
        used = None
        for topic in topics:
            rows = session.exec(
                select(NewsSentiment).where(
                    NewsSentiment.topic == topic, NewsSentiment.date >= since
                )
            ).all()
            if rows:
                used = topic
                break
        if not rows:
            return 0, {"level": "none", "note": "no sentiment in the window", "adjust": 0}
        vals = [r.score / 10 if r.source.startswith("gdelt") else r.score for r in rows]
        mean = sum(vals) / len(vals)
        info = {
            "level": "sector",
            "topic": used,
            "mean": round(mean, 4),
            "n": len(rows),
            "source": rows[-1].source,
            "note": "sector-level sentiment only",
        }
    adj = 1 if mean > SENTIMENT_THRESHOLD else -1 if mean < -SENTIMENT_THRESHOLD else 0
    info["adjust"] = adj
    return adj, info


def score_crowd(
    percentile: float | None, surprise: int, sentiment: int = 0
) -> tuple[float | None, str]:
    """Contrarian at extremes, confirming in the middle. Our decisions are longs, so 'with the crowd' = crowd long.
    Surprise and sentiment only count in the 30-70 band; the result is clamped to 1-5."""
    if percentile is None:
        return None, "no positioning data; neutral used"
    p = percentile
    if p > 90:
        return 1.0, "crowded long (P>90): 1"
    if p < 10:
        return 4.0, "crowded short (P<10): against the crowd: 4"
    if 30 <= p <= 70:
        base = 3.0 + surprise + sentiment
        return (
            float(max(1, min(5, base))),
            f"no positioning information (P {p:.0f}): 3 {surprise:+d} surprise {sentiment:+d} sentiment",
        )
    if p > 70:
        return 2.0, f"mildly stretched long (P {p:.0f}): 2"
    return 3.0, f"mildly stretched short (P {p:.0f}): against: 3"


def crowd_factor(session: Session, inst: Instrument, today: dt.date) -> CrowdResult:
    p, inputs = crowd_long_percentile(session, inst, today)
    ev = last_with_actual(session, today, instrument_id=inst.id, theme=inst.theme)
    surprise = surprise_direction(ev, inst.theme, inst.id) if ev else 0
    if ev:
        inputs["surprise"] = {
            "event": ev.name,
            "date": ev.date.isoformat(),
            "consensus": ev.consensus,
            "actual": ev.actual,
            "direction": surprise,
        }
    sent, sinfo = sentiment_adjust(session, inst, today)
    inputs["sentiment"] = sinfo
    in_band = p is not None and 30 <= p <= 70
    value, note = score_crowd(p, surprise if in_band else 0, sent if in_band else 0)
    inputs["disclaimer"] = DISCLAIMER
    return CrowdResult(value, p, inputs, note)


def consensus_gap(
    session: Session,
    target_key: str | None,
    safra_value: float | None,
    today: dt.date | None = None,
) -> tuple[bool, dict[str, Any]]:
    """True when the Safra target is within 2% of the street consensus (CONSENSUS_TARGET:<key> observation)."""
    if not target_key or not safra_value:
        return False, {}
    obs = session.exec(
        select(Observation)
        .where(Observation.series == f"CONSENSUS_TARGET:{target_key}", Observation.value > 0)
        .order_by(Observation.date.desc())
        .limit(1)
    ).first()
    if obs is None:
        return False, {"note": f"no consensus target for {target_key}"}
    gap = safra_value / obs.value - 1
    return abs(gap) <= 0.02, {
        "key": target_key,
        "safra": safra_value,
        "consensus": obs.value,
        "gap_pct": round(gap * 100, 2),
        "as_of": obs.date.isoformat(),
        "source": obs.source,
    }


def deferral_reason(
    session: Session, inst: Instrument, today: dt.date, percentile: float | None
) -> str | None:
    """BUY/ADD with a scheduled event within 2 trading days and positioning above 80 or below 20 is deferred."""
    if percentile is None or 20 <= percentile <= 80:
        return None
    events = upcoming(session, today, 2, inst.id)
    if not events:
        return None
    names = ", ".join(f"{e.name} ({e.date})" for e in events)
    return f"positioning percentile {percentile:.0f} with {names} within 2 trading days; re-evaluated after the event"
