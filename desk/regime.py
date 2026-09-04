"""Regime classifier (docs/BRIEF.md section 6). Deliberately simple and legible: four dimensions, each a label
with the inputs that produced it. Runs daily and writes one `regime` row."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from sqlmodel import Session, select

from desk.houseviews import all_views
from desk.models import Instrument, Observation, Price, Regime
from desk.sources.base import utcnow

INFLATION_RULE = "energy_shock if headline - core > 0.75pp; broad if core > 3%; contained otherwise"
POLICY_RULE = (
    "hiking / cutting from the 90-day Fed funds trend and the last three house-view rate forecasts"
)
OIL_RULE = (
    "shock if Brent > 90 and > +15% over 60 days; elevated if > 80 or > +15%; normal otherwise"
)
VOL_RULE = "complacent < 15; normal 15-25; stressed > 25"


@dataclass
class Dimension:
    state: str
    inputs: dict[str, Any] = field(default_factory=dict)


def latest_obs(session: Session, series: str) -> Observation | None:
    return session.exec(
        select(Observation)
        .where(Observation.series == series)
        .order_by(Observation.date.desc(), Observation.fetched_at.desc())
        .limit(1)
    ).first()


def obs_at(session: Session, series: str, on: dt.date) -> Observation | None:
    """Most recent observation on or before `on`."""
    return session.exec(
        select(Observation)
        .where(Observation.series == series, Observation.date <= on)
        .order_by(Observation.date.desc())
        .limit(1)
    ).first()


def latest_close(session: Session, ticker: str) -> Price | None:
    inst = session.exec(select(Instrument).where(Instrument.ticker == ticker)).first()
    if inst is None:
        return None
    return session.exec(
        select(Price).where(Price.instrument_id == inst.id).order_by(Price.date.desc()).limit(1)
    ).first()


def close_at(session: Session, ticker: str, on: dt.date) -> Price | None:
    inst = session.exec(select(Instrument).where(Instrument.ticker == ticker)).first()
    if inst is None:
        return None
    return session.exec(
        select(Price)
        .where(Price.instrument_id == inst.id, Price.date <= on)
        .order_by(Price.date.desc())
        .limit(1)
    ).first()


def _yoy(session: Session, series: str) -> tuple[float | None, dt.date | None]:
    """Year-over-year % change of an index level series (CPIAUCSL etc.)."""
    cur = latest_obs(session, series)
    if cur is None:
        return None, None
    prev = obs_at(session, series, cur.date - dt.timedelta(days=350))
    if prev is None or prev.date > cur.date - dt.timedelta(days=330) or prev.value == 0:
        return None, cur.date
    return (cur.value / prev.value - 1) * 100, cur.date


def classify_inflation(session: Session) -> Dimension:
    inputs: dict[str, Any] = {"rule": INFLATION_RULE}
    gaps, cores = [], []
    us_h, us_d = _yoy(session, "CPIAUCSL")
    us_c, _ = _yoy(session, "CPILFESL")
    if us_h is not None and us_c is not None:
        inputs["us"] = {
            "headline_yoy": round(us_h, 2),
            "core_yoy": round(us_c, 2),
            "as_of": us_d.isoformat(),
        }
        gaps.append(us_h - us_c)
        cores.append(us_c)
    else:
        inputs["us"] = "CPI y/y needs 13 months of CPIAUCSL/CPILFESL observations"
    ez_h, ez_c = latest_obs(session, "EZ_HICP"), latest_obs(session, "EZ_HICP_CORE")
    if ez_h and ez_c:
        inputs["ez"] = {
            "headline_yoy": ez_h.value,
            "core_yoy": ez_c.value,
            "as_of": ez_h.date.isoformat(),
            "source": ez_h.source,
        }
        gaps.append(ez_h.value - ez_c.value)
        cores.append(ez_c.value)
    if not gaps:
        return Dimension("unknown", {**inputs, "note": "no inflation data"})
    gap, core = max(gaps), max(cores)
    inputs.update({"headline_minus_core": round(gap, 2), "core": round(core, 2)})
    if gap > 0.75:
        return Dimension("energy_shock", inputs)
    if core > 3:
        return Dimension("broad", inputs)
    return Dimension("contained", inputs)


def classify_policy(session: Session) -> Dimension:
    inputs: dict[str, Any] = {"rule": POLICY_RULE}
    votes: list[str] = []
    dff = latest_obs(session, "DFF")
    if dff is not None:
        past = obs_at(session, "DFF", dff.date - dt.timedelta(days=90))
        if past is not None:
            delta = dff.value - past.value
            inputs["dff"] = {
                "now": dff.value,
                "90d_ago": past.value,
                "delta": round(delta, 3),
                "as_of": dff.date.isoformat(),
            }
            votes.append("hiking" if delta > 0.10 else "cutting" if delta < -0.10 else "on_hold")
    # the last three house-view rate forecasts for the Fed and the ECB versus the current level
    forecasts = []
    for row in all_views(session):
        v = row.view
        if v.scope != "rate" or v.value is None:
            continue
        key = v.key.lower()
        if not (key.startswith("fed funds") or key.startswith("ecb deposit")):
            continue
        try:
            target = float(v.value.replace("%", "").replace("'", ""))
        except ValueError:
            continue
        current = dff.value if key.startswith("fed") else (latest_obs(session, "ECB_DEPO") or dff)
        current_val = current.value if hasattr(current, "value") else None
        if current_val is None:
            continue
        forecasts.append(
            {
                "key": v.key,
                "forecast": target,
                "current": current_val,
                "report_date": row.report.date.isoformat(),
            }
        )
        votes.append(
            "hiking"
            if target > current_val + 0.1
            else "cutting"
            if target < current_val - 0.1
            else "on_hold"
        )
        if len(forecasts) == 3:
            break
    inputs["house_forecasts"] = forecasts
    if not votes:
        return Dimension("unknown", {**inputs, "note": "no policy inputs"})
    inputs["votes"] = votes
    if "hiking" in votes and "cutting" not in votes:
        return Dimension("hiking", inputs)
    if "cutting" in votes and "hiking" not in votes:
        return Dimension("cutting", inputs)
    return Dimension("on_hold", inputs)


def classify_oil(session: Session) -> Dimension:
    inputs: dict[str, Any] = {"rule": OIL_RULE}
    brent = latest_close(session, "BZ=F")
    if brent is None:
        return Dimension("unknown", {**inputs, "note": "no Brent price"})
    past = close_at(session, "BZ=F", brent.date - dt.timedelta(days=60))
    chg = (brent.close / past.close - 1) * 100 if past else None
    inputs.update(
        {
            "brent": brent.close,
            "as_of": brent.date.isoformat(),
            "source": brent.source,
            "change_60d_pct": round(chg, 1) if chg is not None else None,
        }
    )
    if brent.close > 90 and chg is not None and chg > 15:
        return Dimension("shock", inputs)
    if brent.close > 80 or (chg is not None and chg > 15):
        return Dimension("elevated", inputs)
    return Dimension("normal", inputs)


def classify_vol(session: Session) -> Dimension:
    inputs: dict[str, Any] = {"rule": VOL_RULE}
    vix = latest_close(session, "^VIX")
    if vix is None:
        return Dimension("unknown", {**inputs, "note": "no VIX price"})
    inputs.update({"vix": vix.close, "as_of": vix.date.isoformat(), "source": vix.source})
    if vix.close < 15:
        return Dimension("complacent", inputs)
    if vix.close > 25:
        return Dimension("stressed", inputs)
    return Dimension("normal", inputs)


LABELS = {
    "inflation_state": {
        "energy_shock": "Energy-shock inflation",
        "broad": "Broad inflation",
        "contained": "Contained inflation",
        "unknown": "Inflation unknown",
    },
    "policy_state": {
        "hiking": "central banks hiking",
        "cutting": "central banks cutting",
        "on_hold": "central banks on hold",
        "unknown": "policy unknown",
    },
    "oil_state": {
        "shock": "oil shock",
        "elevated": "oil elevated",
        "normal": "oil normal",
        "unknown": "oil unknown",
    },
    "vol_state": {
        "complacent": "equity vol complacent",
        "normal": "equity vol normal",
        "stressed": "equity vol stressed",
        "unknown": "vol unknown",
    },
}


def make_label(inflation: str, policy: str, oil: str, vol: str) -> str:
    return (
        f"{LABELS['inflation_state'][inflation]}, {LABELS['policy_state'][policy]}, "
        f"{LABELS['oil_state'][oil]}, {LABELS['vol_state'][vol]}."
    )


def classify(session: Session, on: dt.date | None = None) -> Regime:
    """Classify and upsert today's regime row. Returns the row."""
    on = on or dt.date.today()
    infl, pol, oil, vol = (
        classify_inflation(session),
        classify_policy(session),
        classify_oil(session),
        classify_vol(session),
    )
    inputs = {
        "inflation": infl.inputs,
        "policy": pol.inputs,
        "oil": oil.inputs,
        "vol": vol.inputs,
        "computed_at": utcnow().isoformat(),
    }
    row = session.exec(select(Regime).where(Regime.date == on)).first()
    if row is None:
        row = Regime(
            date=on, label="", inflation_state="", policy_state="", oil_state="", vol_state=""
        )
    row.inflation_state, row.policy_state, row.oil_state, row.vol_state = (
        infl.state,
        pol.state,
        oil.state,
        vol.state,
    )
    row.label = make_label(infl.state, pol.state, oil.state, vol.state)
    row.inputs_json = inputs
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def latest_regime(session: Session) -> Regime | None:
    return session.exec(select(Regime).order_by(Regime.date.desc()).limit(1)).first()
