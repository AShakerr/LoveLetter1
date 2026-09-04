"""Conviction score (docs/BRIEF.md section 7). Six factors, each 0-5, fixed weights, total 0-100.
Every factor stores its inputs so the UI can render the breakdown."""

from __future__ import annotations

import datetime as dt
import re
import statistics
from dataclasses import dataclass, field
from typing import Any

from sqlmodel import Session, select

from desk.config import Settings, get_settings
from desk.houseviews import ViewRow, all_views
from desk.models import Instrument, InstrumentKind, NewsSentiment, Observation, Price, Regime, Score
from desk.portfolio import PortfolioView
from desk.regime_fit import RegimeFit

WEIGHTS = {
    "safra": 25,
    "regime": 20,
    "portfolio": 15,
    "valuation": 15,
    "momentum": 15,
    "season": 10,
}
BANDS = {"act": 75, "candidate": 60, "watch": 45}
STANCE_SCORE = {
    "most_preferred": 5.0,
    "overweight": 5.0,
    "buy": 5.0,
    "strong_buy": 5.0,
    "neutral": 2.5,
    "hold": 2.5,
    "least_preferred": 0.0,
    "underweight": 0.0,
    "sell": 0.0,
}
CYCLICAL_SECTORS = {"Industrials", "Materials", "Consumer Discretionary", "Information Technology"}
SCORABLE_KINDS = {
    InstrumentKind.stock,
    InstrumentKind.etf,
    InstrumentKind.commodity,
    InstrumentKind.crypto,
    InstrumentKind.private,
}


def band(total: float) -> str:
    if total >= BANDS["act"]:
        return "act"
    if total >= BANDS["candidate"]:
        return "candidate"
    if total >= BANDS["watch"]:
        return "watch"
    return "avoid"


def clamp(v: float, lo: float = 0.0, hi: float = 5.0) -> float:
    return max(lo, min(hi, v))


@dataclass
class Factor:
    value: float | None
    inputs: dict[str, Any] = field(default_factory=dict)
    note: str | None = None

    @property
    def effective(self) -> float:
        return 0.0 if self.value is None else self.value


# ---------------------------------------------------------------------------------------------- helpers
def _current(views: list[ViewRow], scope: str, key: str | None) -> ViewRow | None:
    if not key:
        return None
    for r in views:
        if not r.tactical and r.view.scope == scope and r.view.key == key:
            return r
    return None


def _latest_price(session: Session, instrument_id: int) -> Price | None:
    return session.exec(
        select(Price)
        .where(Price.instrument_id == instrument_id)
        .order_by(Price.date.desc())
        .limit(1)
    ).first()


def _price_at(session: Session, instrument_id: int, on: dt.date) -> Price | None:
    return session.exec(
        select(Price)
        .where(Price.instrument_id == instrument_id, Price.date <= on)
        .order_by(Price.date.desc())
        .limit(1)
    ).first()


def _first_number(text: str | None) -> float | None:
    if not text:
        return None
    m = re.search(r"-?\d[\d',]*(?:\.\d+)?", text)
    if not m:
        return None
    try:
        return float(m.group(0).replace("'", "").replace(",", ""))
    except ValueError:
        return None


def _returns(session: Session, instrument_id: int, since: dt.date) -> dict[dt.date, float]:
    rows = session.exec(
        select(Price)
        .where(Price.instrument_id == instrument_id, Price.date >= since)
        .order_by(Price.date)
    ).all()
    out: dict[dt.date, float] = {}
    prev = None
    for r in rows:
        if prev is not None and prev.close:
            out[r.date] = r.close / prev.close - 1
        prev = r
    return out


def correlation(a: dict[dt.date, float], b: dict[dt.date, float]) -> float | None:
    common = sorted(set(a) & set(b))
    if len(common) < 20:
        return None
    xs, ys = [a[d] for d in common], [b[d] for d in common]
    try:
        return statistics.correlation(xs, ys)
    except statistics.StatisticsError:
        return None


# ---------------------------------------------------------------------------------------------- factors
def f_safra(inst: Instrument, views: list[ViewRow], today: dt.date) -> Factor:
    inputs: dict[str, Any] = {}
    stock = _current(views, "stock", inst.ticker)
    if stock is not None and stock.view.stance in STANCE_SCORE:
        base = STANCE_SCORE[stock.view.stance]
        inputs["stock_rating"] = {
            "stance": stock.view.stance,
            "value": stock.view.value,
            "date": stock.report.date.isoformat(),
        }
        chosen = [stock]
    else:
        comps: dict[str, ViewRow] = {}
        for scope, key in (("sector", inst.sector), ("region", inst.region)):
            r = _current(views, scope, key)
            if r is not None and r.view.stance in STANCE_SCORE:
                comps[scope] = r
        if inst.kind in (InstrumentKind.stock, InstrumentKind.etf):
            r = _current(views, "asset", "Equities")
            if r is not None and r.view.stance in STANCE_SCORE:
                comps["asset"] = r
        if not comps:
            return Factor(
                None,
                {"note": "no house coverage for sector/region/asset"},
                "no house coverage; neutral 2.5 used",
            )
        inputs["components"] = {
            k: {
                "key": r.view.key,
                "stance": r.view.stance,
                "score": STANCE_SCORE[r.view.stance],
                "date": r.report.date.isoformat(),
            }
            for k, r in comps.items()
        }
        base = sum(STANCE_SCORE[r.view.stance] for r in comps.values()) / len(comps)
        chosen = list(comps.values())
    adj = 0.0
    for r in chosen:
        if r.view.changed_from and (today - r.report.date).days <= 30:
            if r.direction == "upgrade":
                adj += 0.5
            elif r.direction == "downgrade":
                adj -= 0.5
    inputs["recent_change_adjustment"] = adj
    return Factor(clamp(base + adj), inputs)


def f_regime(inst: Instrument, regime: Regime | None, fit: RegimeFit | None) -> Factor:
    if fit is None:
        return Factor(
            None,
            {"note": "config/regime_fit.yaml missing"},
            "regime_fit.yaml missing; factor not scored",
        )
    if regime is None:
        return Factor(None, {"note": "no regime row"}, "no regime; factor not scored")
    value, inputs = fit.score(inst.theme, regime)
    inputs["regime"] = regime.label
    if value is None:
        return Factor(
            None, inputs, inputs.get("current", {}).get("note") or "theme not in regime_fit.yaml"
        )
    return Factor(clamp(value), inputs)


def f_portfolio(session: Session, inst: Instrument, view: PortfolioView, today: dt.date) -> Factor:
    themes = {k: v for k, v in view.by_theme.items() if k != "cash"}
    if not themes:
        return Factor(None, {"note": "empty portfolio"}, "empty portfolio; neutral 2.5 used")
    top_theme, top_w = max(themes.items(), key=lambda kv: kv[1])
    theme = inst.theme or "unassigned"
    if theme != top_theme:
        base, why = 5.0, f"adding {theme} reduces the largest theme ({top_theme} {top_w:.0%})"
    elif top_w > 0.35:
        base, why = 1.0, f"{theme} already {top_w:.0%} (> 35%)"
    else:
        base, why = 3.0, f"{theme} is the largest theme at {top_w:.0%}"
    inputs: dict[str, Any] = {
        "largest_theme": top_theme,
        "largest_theme_weight": round(top_w, 4),
        "base": base,
        "why": why,
    }
    # correlation with the largest holding that has price history
    largest = None
    for p in view.positions:
        if p.instrument.id != inst.id and _latest_price(session, p.instrument.id) is not None:
            largest = p
            break
    penalty = 0.0
    if largest is not None:
        since = today - dt.timedelta(days=90)
        rho = correlation(
            _returns(session, inst.id, since), _returns(session, largest.instrument.id, since)
        )
        inputs["largest_holding"] = largest.instrument.ticker
        inputs["corr_90d"] = None if rho is None else round(rho, 3)
        if rho is not None:
            penalty = 2.0 * max(rho, 0.0)
    inputs["correlation_penalty"] = round(penalty, 3)
    return Factor(clamp(base - penalty), inputs)


def f_valuation(
    session: Session, inst: Instrument, cfg: dict[str, Any] | None, views: list[ViewRow]
) -> Factor:
    inputs: dict[str, Any] = {}
    parts: list[float] = []
    cfg = cfg or {}
    pe_series = cfg.get("pe_series")
    if pe_series:
        rows = session.exec(
            select(Observation).where(Observation.series == pe_series).order_by(Observation.date)
        ).all()
        if rows:
            cur = rows[-1].value
            hist = [r.value for r in rows]
            median = statistics.median(hist)
            ratio = cur / median if median else None
            if ratio is not None:
                # 0.8x or cheaper -> 5, 1.0x -> 2.5, 1.2x or dearer -> 0
                pe_score = clamp(2.5 - (ratio - 1) * 12.5)
                parts.append(pe_score)
                inputs["pe"] = {
                    "series": pe_series,
                    "current": cur,
                    "median": median,
                    "n": len(hist),
                    "as_of": rows[-1].date.isoformat(),
                    "ratio": round(ratio, 3),
                    "score": round(pe_score, 2),
                    "note": "history shorter than 5 years" if len(hist) < 60 else None,
                }
    target_key = cfg.get("target_key")
    target_row = (
        _current(views, "index_target", target_key) or _current(views, "commodity", target_key)
        if target_key
        else None
    )
    if target_row is None:
        stock = _current(views, "stock", inst.ticker)
        if stock is not None and _first_number(stock.view.value):
            target_row, target_key = stock, inst.ticker
    if target_row is not None:
        target = _first_number(target_row.view.value)
        ref_ticker = cfg.get("index_ticker") or inst.ticker
        ref = session.exec(select(Instrument).where(Instrument.ticker == ref_ticker)).first()
        px = _latest_price(session, ref.id) if ref else None
        if target and px and px.close:
            upside = target / px.close - 1
            # -10% -> 0, 0 -> 2, +5% -> 3, +15% -> 5
            t_score = clamp(2 + upside * 20) if upside >= 0 else clamp(2 + upside * 20)
            parts.append(t_score)
            inputs["target"] = {
                "key": target_key,
                "target": target,
                "current": px.close,
                "ref": ref_ticker,
                "as_of": px.date.isoformat(),
                "upside_pct": round(upside * 100, 1),
                "score": round(t_score, 2),
                "report_date": target_row.report.date.isoformat(),
            }
    if not parts:
        return Factor(
            None,
            {"note": "no valuation reference (no P/E series, target or rating)"},
            "no valuation reference; neutral 2.5 used",
        )
    return Factor(clamp(sum(parts) / len(parts)), inputs)


def three_month_return(
    session: Session, instrument_id: int, today: dt.date
) -> tuple[float | None, dict[str, Any]]:
    now = _latest_price(session, instrument_id)
    if now is None:
        return None, {"note": "no prices"}
    past = _price_at(session, instrument_id, now.date - dt.timedelta(days=91))
    if past is None or not past.close:
        return None, {"note": "less than 3 months of prices"}
    return now.close / past.close - 1, {
        "from": past.date.isoformat(),
        "to": now.date.isoformat(),
        "from_close": past.close,
        "to_close": now.close,
    }


def sentiment_14d(session: Session, instrument_id: int, today: dt.date) -> tuple[float | None, int]:
    rows = session.exec(
        select(NewsSentiment).where(
            NewsSentiment.instrument_id == instrument_id,
            NewsSentiment.date >= today - dt.timedelta(days=14),
        )
    ).all()
    if not rows:
        return None, 0
    return sum(r.score for r in rows) / len(rows), len(rows)


def f_momentum(
    session: Session, inst: Instrument, universe_returns: dict[int, float], today: dt.date
) -> Factor:
    inputs: dict[str, Any] = {}
    parts = []
    ret, detail = three_month_return(session, inst.id, today)
    if ret is not None and universe_returns:
        below = sum(1 for v in universe_returns.values() if v < ret)
        pct = below / max(len(universe_returns) - 1, 1)
        m = clamp(pct * 5)
        parts.append(m)
        inputs["return_3m"] = {
            **detail,
            "return_pct": round(ret * 100, 2),
            "percentile": round(pct, 3),
            "universe_n": len(universe_returns),
            "score": round(m, 2),
        }
    else:
        inputs["return_3m"] = detail
    s, n = sentiment_14d(session, inst.id, today)
    if s is not None:
        sc = clamp(2.5 + s / 0.35 * 2.5)
        parts.append(sc)
        inputs["sentiment_14d"] = {"mean": round(s, 4), "n": n, "score": round(sc, 2)}
    else:
        inputs["sentiment_14d"] = {"note": "no ticker sentiment in 14 days"}
    if not parts:
        return Factor(None, inputs, "no momentum inputs; neutral 2.5 used")
    return Factor(sum(parts) / len(parts), inputs)


def f_season(inst: Instrument, today: dt.date) -> Factor:
    inputs: dict[str, Any] = {"month": today.month, "evidence": "docs/seasonality_evidence.md"}
    if today.month not in (11, 12):
        return Factor(0.0, {**inputs, "rule": "outside Nov-Dec"})
    broad = inst.kind == InstrumentKind.etf and (inst.sector or "") == "Broad"
    cyclical = (inst.sector or "") in CYCLICAL_SECTORS
    score = 0.0
    if broad or cyclical:
        score += 3
        inputs["nov_dec_concentration"] = 3
    if cyclical:
        score += 2
        inputs["cyclical_tilt"] = 2
    return Factor(clamp(score), inputs)


# ---------------------------------------------------------------------------------------------- driver
@dataclass
class ScoreResult:
    row: Score
    factors: dict[str, Factor]
    provisional: bool
    notes: list[str]

    @property
    def band(self) -> str:
        return band(self.row.total)


def compute_score(
    session: Session,
    inst: Instrument,
    *,
    regime: Regime | None,
    fit: RegimeFit | None,
    view: PortfolioView,
    views: list[ViewRow],
    universe_returns: dict[int, float],
    valuation_cfg: dict[str, Any] | None,
    today: dt.date,
) -> ScoreResult:
    factors = {
        "safra": f_safra(inst, views, today),
        "regime": f_regime(inst, regime, fit),
        "portfolio": f_portfolio(session, inst, view, today),
        "valuation": f_valuation(session, inst, valuation_cfg, views),
        "momentum": f_momentum(session, inst, universe_returns, today),
        "season": f_season(inst, today),
    }
    # missing inputs are neutral, except regime which stays unscored until the user's table exists
    notes = []
    for name, f in factors.items():
        if f.value is None and name != "regime":
            f.value = 2.5
        if f.note:
            notes.append(f"{name}: {f.note}")
    provisional = factors["regime"].value is None
    total = sum(f.effective / 5 * WEIGHTS[k] for k, f in factors.items())
    row = Score(
        instrument_id=inst.id,
        date=today,
        total=round(total, 2),
        f_safra=factors["safra"].effective,
        f_regime=factors["regime"].effective,
        f_portfolio=factors["portfolio"].effective,
        f_valuation=factors["valuation"].effective,
        f_momentum=factors["momentum"].effective,
        f_season=factors["season"].effective,
        inputs_json={
            "weights": WEIGHTS,
            "band": band(total),
            "provisional": provisional,
            "notes": notes,
            "factors": {k: {"value": f.value, "inputs": f.inputs} for k, f in factors.items()},
            "regime": regime.label if regime else None,
        },
    )
    return ScoreResult(row, factors, provisional, notes)


def score_universe(
    session: Session,
    view: PortfolioView,
    regime: Regime | None,
    *,
    settings: Settings | None = None,
    today: dt.date | None = None,
    universe: list[dict] | None = None,
) -> list[ScoreResult]:
    """Score every scorable instrument and upsert the rows for `today`."""
    from desk.universe import load_universe

    settings = settings or get_settings()
    today = today or dt.date.today()
    universe = (
        universe if universe is not None else load_universe(settings.config_dir / "universe.yaml")
    )
    val_cfg = {u["ticker"]: u.get("valuation") for u in universe}
    fit = RegimeFit.load(settings.config_dir / "regime_fit.yaml")
    views = all_views(session)
    instruments = [i for i in session.exec(select(Instrument)).all() if i.kind in SCORABLE_KINDS]
    universe_returns: dict[int, float] = {}
    for i in instruments:
        r, _ = three_month_return(session, i.id, today)
        if r is not None:
            universe_returns[i.id] = r
    results = []
    for inst in instruments:
        res = compute_score(
            session,
            inst,
            regime=regime,
            fit=fit,
            view=view,
            views=views,
            universe_returns=universe_returns,
            valuation_cfg=val_cfg.get(inst.ticker),
            today=today,
        )
        existing = session.exec(
            select(Score).where(Score.instrument_id == inst.id, Score.date == today)
        ).first()
        if existing is not None:
            for k in (
                "total",
                "f_safra",
                "f_regime",
                "f_portfolio",
                "f_valuation",
                "f_momentum",
                "f_season",
                "inputs_json",
            ):
                setattr(existing, k, getattr(res.row, k))
            res.row = existing
        session.add(res.row)
        results.append(res)
    session.commit()
    for r in results:
        session.refresh(r.row)
    return results


def score_history(session: Session, instrument_id: int, since: dt.date) -> list[Score]:
    return list(
        session.exec(
            select(Score)
            .where(Score.instrument_id == instrument_id, Score.date >= since)
            .order_by(Score.date)
        ).all()
    )
