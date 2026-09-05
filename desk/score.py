"""Conviction score (docs/BRIEF.md 7, 7b, 7c). Seven factors, each 0-5, fixed weights, total 0-100.
Every factor stores its inputs so the UI can render the breakdown."""

from __future__ import annotations

import datetime as dt
import re
import statistics
from dataclasses import dataclass, field
from typing import Any

from sqlmodel import Session, select

from desk.config import Settings, get_settings
from desk.crowd import consensus_gap, crowd_factor
from desk.fundamentals import history_median, latest_fundamentals, sector_stats
from desk.houseviews import ViewRow, all_views
from desk.models import Instrument, InstrumentKind, NewsSentiment, Observation, Price, Regime, Score
from desk.portfolio import PortfolioView
from desk.regime_fit import RegimeFit
from desk.valuation import ValuationResult, score_etf, score_stock

WEIGHTS = {
    "safra": 25,
    "regime": 20,
    "portfolio": 15,
    "valuation": 15,
    "momentum": 10,
    "crowd": 10,
    "season": 5,
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
CYCLICAL_SECTORS = {
    "Industrials",
    "Materials",
    "Consumer Discretionary",
    "Information Technology",
    "Technology",
    "Consumer Cyclical",
    "Basic Materials",
}
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
    try:
        return statistics.correlation([a[d] for d in common], [b[d] for d in common])
    except statistics.StatisticsError:
        return None


class ValuationContext:
    """Per-run cache of fundamentals-derived inputs (sector medians, latest values, own history)."""

    def __init__(self, session: Session, today: dt.date) -> None:
        self.session, self.today = session, today
        self.sectors = sector_stats(session, "forwardPE")
        self._latest: dict[int, dict] = {}

    def latest(self, instrument_id: int) -> dict[str, tuple[float | None, dt.date, str]]:
        if instrument_id not in self._latest:
            self._latest[instrument_id] = latest_fundamentals(self.session, instrument_id)
        return self._latest[instrument_id]

    def values(self, instrument_id: int) -> dict[str, float | None]:
        return {k: v[0] for k, v in self.latest(instrument_id).items()}

    def as_of(self, instrument_id: int) -> dict[str, Any]:
        lat = self.latest(instrument_id)
        if not lat:
            return {}
        d = max(v[1] for v in lat.values())
        return {"date": d.isoformat(), "sources": sorted({v[2] for v in lat.values()})}

    def own_history(self, instrument_id: int) -> tuple[float | None, int]:
        return history_median(self.session, instrument_id, "trailingPE", 5, self.today)


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
        note = (
            inputs.get("current", {}).get("note") or f"theme {inst.theme!r} not in regime_fit.yaml"
        )
        return Factor(2.5, inputs, note + "; neutral 2.5 used")
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


def _target_score(
    session: Session, inst: Instrument, cfg: dict[str, Any], views: list[ViewRow]
) -> tuple[float | None, dict[str, Any]]:
    """Distance to the Safra target (index/commodity target or focus-list TP): -10% -> 0, 0 -> 2, +15% -> 5."""
    target_key = cfg.get("target_key")
    target_row = (
        (_current(views, "index_target", target_key) or _current(views, "commodity", target_key))
        if target_key
        else None
    )
    if target_row is None:
        stock = _current(views, "stock", inst.ticker)
        if stock is not None and _first_number(stock.view.value):
            target_row, target_key = stock, inst.ticker
    if target_row is None:
        return None, {}
    target = _first_number(target_row.view.value)
    ref_ticker = cfg.get("index_ticker") or inst.ticker
    ref = session.exec(select(Instrument).where(Instrument.ticker == ref_ticker)).first()
    px = _latest_price(session, ref.id) if ref else None
    if not (target and px and px.close):
        return None, {}
    upside = target / px.close - 1
    score = clamp(2 + upside * 20)
    return score, {
        "key": target_key,
        "target": target,
        "current": px.close,
        "ref": ref_ticker,
        "as_of": px.date.isoformat(),
        "upside_pct": round(upside * 100, 1),
        "score": round(score, 2),
        "report_date": target_row.report.date.isoformat(),
    }


def f_valuation(
    session: Session,
    inst: Instrument,
    cfg: dict[str, Any] | None,
    views: list[ViewRow],
    vctx: ValuationContext | None,
) -> tuple[Factor, ValuationResult | None]:
    cfg = cfg or {}
    inputs: dict[str, Any] = {}
    vres: ValuationResult | None = None
    if inst.kind == InstrumentKind.stock and vctx is not None:
        f = vctx.values(inst.id)
        if any(v is not None for v in f.values()):
            vres = score_stock(
                f,
                vctx.sectors.get(inst.sector or ""),
                vctx.own_history(inst.id),
                vctx.as_of(inst.id),
            )
            inputs["fundamentals"] = vres.inputs
            inputs["flags"] = vres.flags
            if vres.value is not None:
                return Factor(clamp(vres.value), inputs), vres
    if inst.kind in (InstrumentKind.etf, InstrumentKind.index):
        pe_series = cfg.get("pe_series")
        hist = []
        if pe_series:
            rows = session.exec(
                select(Observation)
                .where(Observation.series == pe_series)
                .order_by(Observation.date)
            ).all()
            hist = [(r.date, r.value) for r in rows]
        fund_pe = vctx.values(inst.id).get("trailingPE") if vctx is not None else None
        if hist:
            vres = score_etf(hist[-1][1], hist, fund_pe)
            inputs["pe"] = {"series": pe_series, **vres.inputs}
            if vres.value is not None:
                return Factor(clamp(vres.value), inputs), vres
    t_score, t_inputs = _target_score(session, inst, cfg, views)
    if t_score is not None:
        inputs["target"] = t_inputs
        return Factor(t_score, inputs), vres
    return Factor(
        None,
        {**inputs, "note": "no valuation reference (no fundamentals, P/E series or target)"},
        "no valuation reference; neutral 2.5 used",
    ), vres


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
    """Pure price trend (7b): 3-month return percentile within the universe. Sentiment is recorded, not scored."""
    inputs: dict[str, Any] = {}
    ret, detail = three_month_return(session, inst.id, today)
    s, n = sentiment_14d(session, inst.id, today)
    inputs["sentiment_14d"] = (
        {"mean": round(s, 4), "n": n, "note": "recorded for momentum_break; not scored"}
        if s is not None
        else {"note": "no ticker sentiment in 14 days"}
    )
    if ret is None or not universe_returns:
        inputs["return_3m"] = detail
        return Factor(None, inputs, "no price momentum; neutral 2.5 used")
    below = sum(1 for v in universe_returns.values() if v < ret)
    pct = below / max(len(universe_returns) - 1, 1)
    inputs["return_3m"] = {
        **detail,
        "return_pct": round(ret * 100, 2),
        "percentile": round(pct, 3),
        "universe_n": len(universe_returns),
    }
    return Factor(clamp(pct * 5), inputs)


def f_crowd(session: Session, inst: Instrument, today: dt.date) -> Factor:
    res = crowd_factor(session, inst, today)
    inputs = dict(res.inputs)
    inputs["percentile"] = None if res.percentile is None else round(res.percentile, 1)
    inputs["basis"] = res.note
    if res.value is None:
        return Factor(None, inputs, res.note)
    return Factor(clamp(res.value), inputs)


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
    flags: list[str] = field(default_factory=list)
    total_cap: float | None = None

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
    vctx: ValuationContext | None = None,
) -> ScoreResult:
    val_factor, vres = f_valuation(session, inst, valuation_cfg, views, vctx)
    factors = {
        "safra": f_safra(inst, views, today),
        "regime": f_regime(inst, regime, fit),
        "portfolio": f_portfolio(session, inst, view, today),
        "valuation": val_factor,
        "momentum": f_momentum(session, inst, universe_returns, today),
        "crowd": f_crowd(session, inst, today),
        "season": f_season(inst, today),
    }
    notes: list[str] = []
    flags: list[str] = list(vres.flags) if vres else []
    # consensus gap (7b): a house view that is consensus caps Safra alignment at 4/5
    cfg = valuation_cfg or {}
    if cfg.get("target_key") and factors["safra"].value is not None:
        row = _current(views, "index_target", cfg["target_key"]) or _current(
            views, "commodity", cfg["target_key"]
        )
        safra_value = _first_number(row.view.value) if row else None
        within, cg = consensus_gap(session, cfg["target_key"], safra_value, today)
        if cg:
            factors["safra"].inputs["consensus_gap"] = cg
        if within:
            factors["safra"].value = min(factors["safra"].value, 4.0)
            factors["safra"].inputs["cap"] = "house view is consensus: capped at 4/5"
            notes.append("safra: house view is consensus (within 2% of the street); capped at 4/5")
    for name, f in factors.items():
        if f.value is None and name != "regime":
            f.value = 2.5
        if f.note:
            notes.append(f"{name}: {f.note}")
    provisional = factors["regime"].value is None
    total = sum(f.effective / 5 * WEIGHTS[k] for k, f in factors.items())
    cap = vres.total_cap if vres else None
    if cap is not None and total > cap:
        notes.append(f"valuation: no earnings; total capped at {cap:.0f}")
        total = cap
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
        f_crowd=factors["crowd"].effective,
        inputs_json={
            "weights": WEIGHTS,
            "band": band(total),
            "provisional": provisional,
            "notes": notes,
            "flags": flags,
            "total_cap": cap,
            "factors": {k: {"value": f.value, "inputs": f.inputs} for k, f in factors.items()},
            "regime": regime.label if regime else None,
            "crowd_percentile": factors["crowd"].inputs.get("percentile"),
        },
    )
    return ScoreResult(row, factors, provisional, notes, flags, cap)


def _upsert(session: Session, res: ScoreResult, inst: Instrument, today: dt.date) -> None:
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
            "f_crowd",
            "inputs_json",
        ):
            setattr(existing, k, getattr(res.row, k))
        res.row = existing
    session.add(res.row)


def score_universe(
    session: Session,
    view: PortfolioView,
    regime: Regime | None,
    *,
    settings: Settings | None = None,
    today: dt.date | None = None,
    universe: list[dict] | None = None,
    instruments: list[Instrument] | None = None,
    persist: bool = True,
) -> list[ScoreResult]:
    """Score every tradable or held instrument (or the given list) and upsert the rows for `today`."""
    from desk.universe import load_universe

    settings = settings or get_settings()
    today = today or dt.date.today()
    universe = (
        universe if universe is not None else load_universe(settings.config_dir / "universe.yaml")
    )
    val_cfg = {u["ticker"]: u.get("valuation") for u in universe}
    fit = RegimeFit.load(settings.config_dir / "regime_fit.yaml")
    views = all_views(session)
    vctx = ValuationContext(session, today)
    if instruments is None:
        held_ids = {p.instrument.id for p in view.positions}
        instruments = [
            i
            for i in session.exec(select(Instrument)).all()
            if i.kind in SCORABLE_KINDS
            and (i.tradable or i.id in held_ids)
            and not i.screener_member
        ]
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
            vctx=vctx,
        )
        if persist:
            _upsert(session, res, inst, today)
        results.append(res)
    if persist:
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
