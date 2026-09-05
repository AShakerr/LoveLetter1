"""Daily screener (docs/BRIEF.md 8c): rank a defined universe by the same conviction score, apply the quality and
value-trap gates, keep the top and bottom 15. Nothing here is a decision until "Propose BUY" is pressed."""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any

import yaml
from sqlmodel import Session, select

from desk.config import Settings, get_settings
from desk.houseviews import all_views, current_stance
from desk.models import (
    Decision,
    Instrument,
    InstrumentKind,
    NewsSentiment,
    Position,
    Regime,
    ScreenerRow,
)
from desk.portfolio import build_portfolio
from desk.regime import latest_regime
from desk.regime_fit import RegimeFit
from desk.score import ValuationContext, compute_score, three_month_return
from desk.sources.base import utcnow
from desk.valuation import quality_gate

log = logging.getLogger(__name__)

WIKI = {
    "sp500": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
    "stoxx600": "https://en.wikipedia.org/wiki/STOXX_Europe_600",
}
SECTOR_MAP = {  # GICS names -> the Safra sector keys used in house views
    "Information Technology": "Information Technology",
    "Technology": "Information Technology",
    "Health Care": "Healthcare",
    "Healthcare": "Healthcare",
    "Financials": "Banks",
    "Financial Services": "Banks",
    "Consumer Discretionary": "Consumer Discretionary",
    "Consumer Cyclical": "Consumer Discretionary",
    "Consumer Staples": "Consumer Staples",
    "Consumer Defensive": "Consumer Staples",
    "Industrials": "Industrials",
    "Energy": "Energy",
    "Materials": "Materials",
    "Basic Materials": "Materials",
    "Utilities": "Utilities",
    "Real Estate": "Real Estate",
    "Communication Services": "Communication Services",
}
THEME_BY_SECTOR = {
    "Information Technology": "ai_capex",
    "Industrials": "industrials",
    "Materials": "materials_copper",
    "Energy": "energy",
    "Healthcare": "healthcare",
    "Banks": "financials",
    "Utilities": "utilities_power",
    "Communication Services": "us_broad",
    "Consumer Discretionary": "us_broad",
    "Consumer Staples": "us_broad",
    "Real Estate": "financials",
}


@dataclass
class ScreenerConfig:
    sources: list[str] = field(default_factory=lambda: ["sp500", "stoxx600", "safra_focus_list"])
    tradable_default: dict[str, bool] = field(
        default_factory=lambda: {"sp500": True, "stoxx600": False, "safra_focus_list": True}
    )
    tradable_overrides: dict[str, bool] = field(default_factory=dict)
    top_n: int = 15
    anti_churn_days: int = 3
    sentiment_calls_per_day: int = 20

    @classmethod
    def load(cls, settings: Settings) -> ScreenerConfig:
        doc = (
            yaml.safe_load((settings.config_dir / "universe.yaml").read_text(encoding="utf-8"))
            or {}
        )
        sc = doc.get("screener") or {}
        cfg = cls()
        for k in (
            "sources",
            "tradable_default",
            "tradable_overrides",
            "top_n",
            "anti_churn_days",
            "sentiment_calls_per_day",
        ):
            if k in sc:
                setattr(cfg, k, sc[k])
        cfg.tradable_overrides = {
            str(k).upper(): bool(v) for k, v in (cfg.tradable_overrides or {}).items()
        }
        return cfg


# ------------------------------------------------------------------------------------- constituents
WIKI_USER_AGENT = "desk/0.1 (private investment-desk tool; python-httpx) constituent refresh"


def _read_html_tables(url: str):
    """Wikipedia answers 403 to urllib's default agent, which is what pandas.read_html(url) uses."""
    from io import StringIO

    import httpx
    import pandas as pd

    r = httpx.get(url, headers={"User-Agent": WIKI_USER_AGENT}, timeout=30, follow_redirects=True)
    r.raise_for_status()
    return pd.read_html(StringIO(r.text))


def fetch_wikipedia_constituents(source: str) -> list[dict[str, Any]]:
    """[{ticker, name, sector, exchange, region, currency, source_symbol}] from the Wikipedia table (network)."""
    tables = _read_html_tables(WIKI[source])
    out: list[dict[str, Any]] = []
    if source == "sp500":
        df = tables[0]
        for _, r in df.iterrows():
            sym = str(r.get("Symbol", "")).strip()
            if not sym or sym == "nan":
                continue
            out.append(
                {
                    "ticker": sym.replace(".", "-"),
                    "name": str(r.get("Security", sym)),
                    "sector": str(r.get("GICS Sector", "")),
                    "exchange": "NYSE/NASDAQ",
                    "region": "USA",
                    "currency": "USD",
                    "source_symbol": sym.replace(".", "-"),
                }
            )
    else:
        df = next(
            (t for t in tables if any("Ticker" in str(c) or "Symbol" in str(c) for c in t.columns)),
            None,
        )
        if df is None:
            return out
        tcol = next(c for c in df.columns if "Ticker" in str(c) or "Symbol" in str(c))
        ncol = next((c for c in df.columns if "Name" in str(c) or "Company" in str(c)), tcol)
        scol = next((c for c in df.columns if "Sector" in str(c) or "Industry" in str(c)), None)
        for _, r in df.iterrows():
            sym = str(r.get(tcol, "")).strip()
            if not sym or sym == "nan":
                continue
            out.append(
                {
                    "ticker": sym.upper(),
                    "name": str(r.get(ncol, sym)),
                    "sector": str(r.get(scol, "")) if scol else "",
                    "exchange": "Europe",
                    "region": "Euro area",
                    "currency": "EUR",
                    "source_symbol": None,
                }
            )
    return out


def safra_focus_list(session: Session) -> list[dict[str, Any]]:
    out = []
    for row in all_views(session):
        if row.view.scope == "stock" and row.report.kind == "equity_focus_list":
            out.append(
                {
                    "ticker": row.view.key.upper(),
                    "name": row.view.key,
                    "sector": "",
                    "exchange": "NYSE/NASDAQ",
                    "region": "USA",
                    "currency": "USD",
                    "source_symbol": row.view.key,
                }
            )
    return out


def refresh_constituents(
    session: Session,
    settings: Settings | None = None,
    fetch=fetch_wikipedia_constituents,
    only: list[str] | None = None,
) -> dict[str, Any]:
    """Upsert screener members into `instruments`; flag names that dropped out. Core universe names are untouched."""
    settings = settings or get_settings()
    cfg = ScreenerConfig.load(settings)
    summary: dict[str, Any] = {}
    for source in only or cfg.sources:
        try:
            rows = safra_focus_list(session) if source == "safra_focus_list" else fetch(source)
        except Exception as exc:  # noqa: BLE001
            log.exception("constituents %s failed", source)
            summary[source] = {"status": "failed", "error": str(exc)}
            continue
        seen: set[str] = set()
        added = updated = 0
        for r in rows:
            t = r["ticker"]
            seen.add(t)
            inst = session.exec(select(Instrument).where(Instrument.ticker == t)).first()
            sector = SECTOR_MAP.get(r.get("sector") or "", r.get("sector") or None) or None
            tradable = cfg.tradable_overrides.get(t, bool(cfg.tradable_default.get(source, False)))
            if inst is None:
                session.add(
                    Instrument(
                        ticker=t,
                        name=r.get("name") or t,
                        kind=InstrumentKind.stock,
                        currency=r.get("currency", "USD"),
                        exchange=r.get("exchange"),
                        tradable=tradable,
                        sector=sector,
                        region=r.get("region"),
                        theme=THEME_BY_SECTOR.get(
                            sector or "", "us_broad" if r.get("region") == "USA" else "eu_broad"
                        ),
                        source_symbol=r.get("source_symbol"),
                        screener_member=source,
                    )
                )
                added += 1
            elif inst.screener_member is not None:  # only rows the screener owns
                changed = False
                if inst.screener_dropped:
                    inst.screener_dropped, changed = False, True
                if not inst.sector and sector:
                    inst.sector, changed = sector, True
                if changed:
                    session.add(inst)
                    updated += 1
        dropped = 0
        for inst in session.exec(
            select(Instrument).where(Instrument.screener_member == source)
        ).all():
            if inst.ticker not in seen and not inst.screener_dropped:
                inst.screener_dropped = True
                session.add(inst)
                dropped += 1
        session.commit()
        summary[source] = {
            "status": "ok",
            "members": len(seen),
            "added": added,
            "updated": updated,
            "dropped": dropped,
        }
    return summary


def screener_instruments(session: Session) -> list[Instrument]:
    return list(
        session.exec(
            select(Instrument).where(
                Instrument.screener_member.is_not(None),
                Instrument.screener_dropped.is_(False),
                Instrument.tradable.is_(True),
            )
        ).all()
    )


# ------------------------------------------------------------------------------------------ pipeline
def sentiment_source(session: Session, inst: Instrument, today: dt.date) -> dict[str, Any]:
    since = today - dt.timedelta(days=14)
    own = session.exec(
        select(NewsSentiment)
        .where(NewsSentiment.instrument_id == inst.id, NewsSentiment.date >= since)
        .order_by(NewsSentiment.date.desc())
    ).all()
    if own:
        return {
            "level": "ticker",
            "mean": round(sum(r.score for r in own) / len(own), 4),
            "n": len(own),
            "as_of": own[0].date.isoformat(),
            "source": own[0].source,
        }
    topic = "energy_transportation" if (inst.sector or "") == "Energy" else "financial_markets"
    rows = session.exec(
        select(NewsSentiment)
        .where(NewsSentiment.topic == topic, NewsSentiment.date >= since)
        .order_by(NewsSentiment.date.desc())
    ).all()
    if rows:
        return {
            "level": "sector",
            "topic": topic,
            "mean": round(sum(r.score for r in rows) / len(rows), 4),
            "n": len(rows),
            "as_of": rows[0].date.isoformat(),
            "source": rows[0].source,
            "note": "sector-level sentiment only",
        }
    return {"level": "none", "note": "no sentiment"}


def run_screener(
    session: Session,
    settings: Settings | None = None,
    today: dt.date | None = None,
    instruments: list[Instrument] | None = None,
) -> dict[str, Any]:
    """Score the screener universe, apply gates, write the top and bottom N rows for the day."""
    settings = settings or get_settings()
    today = today or dt.date.today()
    cfg = ScreenerConfig.load(settings)
    instruments = instruments if instruments is not None else screener_instruments(session)
    if not instruments:
        return {
            "date": today.isoformat(),
            "scored": 0,
            "note": "screener universe is empty: run `desk screener refresh`",
        }
    view = build_portfolio(session, settings)
    regime = latest_regime(session)
    fit = RegimeFit.load(settings.config_dir / "regime_fit.yaml")
    views = all_views(session)
    vctx = ValuationContext(session, today)
    universe_returns: dict[int, float] = {}
    for i in instruments:
        r, _ = three_month_return(session, i.id, today)
        if r is not None:
            universe_returns[i.id] = r
    scored = []
    for inst in instruments:
        res = compute_score(
            session,
            inst,
            regime=regime,
            fit=fit,
            view=view,
            views=views,
            universe_returns=universe_returns,
            valuation_cfg=None,
            today=today,
            vctx=vctx,
        )
        f = vctx.values(inst.id)
        passed, reasons = quality_gate(f, inst.sector)
        gates = {
            "quality": passed,
            "quality_reasons": reasons,
            "value_trap": any("value trap" in x for x in res.flags),
            "no_earnings": any("no earnings" in x for x in res.flags),
            "fundamentals_as_of": vctx.as_of(inst.id).get("date"),
        }
        gates["passed"] = passed and not gates["value_trap"] and not gates["no_earnings"]
        scored.append((inst, res, gates))
    scored.sort(key=lambda t: -t[1].row.total)
    held = {p.instrument.id for p in view.positions}
    top = scored[: cfg.top_n]
    bottom = scored[-cfg.top_n :] if len(scored) > cfg.top_n else []
    keep = {inst.id: (inst, res, gates) for inst, res, gates in top + bottom}
    for inst, res, gates in scored:
        if inst.id in held and res.row.total < 45:
            keep[inst.id] = (inst, res, gates)
    for old in session.exec(select(ScreenerRow).where(ScreenerRow.date == today)).all():
        session.delete(old)
    session.commit()
    rank_of = {inst.id: i + 1 for i, (inst, _, _) in enumerate(scored)}
    for inst, res, gates in keep.values():
        session.add(
            ScreenerRow(
                date=today,
                instrument_id=inst.id,
                rank=rank_of[inst.id],
                total=res.row.total,
                factors_json={
                    "factors": {
                        k: {"value": f.value, "inputs": f.inputs} for k, f in res.factors.items()
                    },
                    "notes": res.notes,
                    "flags": res.flags,
                    "band": res.band,
                    "held": inst.id in held,
                    "sentiment": sentiment_source(session, inst, today),
                    "fundamentals": {k: v[0] for k, v in vctx.latest(inst.id).items()},
                    "safra": _safra_view(session, inst),
                },
                gates_json=gates,
            )
        )
    session.commit()
    return {
        "date": today.isoformat(),
        "scored": len(scored),
        "written": len(keep),
        "top": [(i.ticker, r.row.total, g["passed"]) for i, r, g in top],
    }


def _safra_view(session: Session, inst: Instrument) -> dict[str, Any] | None:
    row = current_stance(session, "stock", inst.ticker) or (
        current_stance(session, "sector", inst.sector) if inst.sector else None
    )
    if row is None:
        return None
    return {
        "scope": row.view.scope,
        "key": row.view.key,
        "stance": row.view.stance,
        "value": row.view.value,
        "date": row.report.date.isoformat(),
        "quote": row.view.quote,
    }


def sentiment_targets(
    session: Session, settings: Settings | None = None, today: dt.date | None = None
) -> list[str]:
    """Tickers that get Alpha Vantage calls today: top N screener names by pre-sentiment score plus anything held."""
    settings = settings or get_settings()
    today = today or dt.date.today()
    cfg = ScreenerConfig.load(settings)
    rows = session.exec(
        select(ScreenerRow).where(ScreenerRow.date == today).order_by(ScreenerRow.rank)
    ).all()
    top = [
        session.get(Instrument, r.instrument_id).ticker for r in rows[: cfg.sentiment_calls_per_day]
    ]
    held = [
        session.get(Instrument, p.instrument_id).ticker
        for p in session.exec(
            select(Position).where(
                Position.confirmed_by_user.is_(True),
                Position.closed_at.is_(None),
                Position.broker == "manual",
            )
        ).all()
    ]
    out: list[str] = []
    for t in top + held:
        if t not in out:
            out.append(t)
    return out


# ------------------------------------------------------------------------------------------- read model
def days_in_top(
    session: Session, instrument_id: int, today: dt.date, top_n: int, lookback: int = 10
) -> int:
    """Consecutive trading days (screener runs) the name has been in the top N, ending today."""
    dates = sorted(
        {r.date for r in session.exec(select(ScreenerRow).where(ScreenerRow.date <= today)).all()},
        reverse=True,
    )[:lookback]
    streak = 0
    for d in dates:
        row = session.exec(
            select(ScreenerRow).where(
                ScreenerRow.date == d, ScreenerRow.instrument_id == instrument_id
            )
        ).first()
        if row is None or row.rank > top_n:
            break
        streak += 1
    return streak


def page_rows(
    session: Session, settings: Settings | None = None, today: dt.date | None = None
) -> dict[str, Any]:
    settings = settings or get_settings()
    cfg = ScreenerConfig.load(settings)
    rows = session.exec(select(ScreenerRow).order_by(ScreenerRow.date.desc())).all()
    if not rows:
        return {"date": None, "candidates": [], "avoid": [], "cfg": cfg}
    day = today or rows[0].date
    rows = [r for r in rows if r.date == day]
    out_c, out_a = [], []
    for r in sorted(rows, key=lambda r: r.rank):
        inst = session.get(Instrument, r.instrument_id)
        fj = r.factors_json or {}
        streak = days_in_top(session, inst.id, day, cfg.top_n)
        port_fit = ((fj.get("factors") or {}).get("portfolio") or {}).get("value") or 0
        proposed = session.exec(
            select(Decision).where(
                Decision.instrument_id == inst.id, Decision.date == day, Decision.action == "BUY"
            )
        ).first()
        item = {
            "row": r,
            "inst": inst,
            "factors": fj.get("factors") or {},
            "notes": fj.get("notes") or [],
            "flags": fj.get("flags") or [],
            "gates": r.gates_json or {},
            "sentiment": fj.get("sentiment") or {},
            "fundamentals": fj.get("fundamentals") or {},
            "safra": fj.get("safra"),
            "held": fj.get("held"),
            "streak": streak,
            "can_propose": r.total >= 75
            and port_fit >= 3
            and (r.gates_json or {}).get("passed")
            and streak >= cfg.anti_churn_days
            and proposed is None
            and inst.tradable,
            "proposed": proposed,
        }
        if r.rank <= cfg.top_n and (r.gates_json or {}).get("passed"):
            out_c.append(item)
        elif r.rank <= cfg.top_n:
            out_c.append(item)  # in the top 15 but gated: shown with the reason
        else:
            out_a.append(item)
    return {"date": day, "candidates": out_c, "avoid": out_a, "cfg": cfg}


def propose_buy(
    session: Session,
    instrument_id: int,
    settings: Settings | None = None,
    today: dt.date | None = None,
) -> Decision:
    """Create a normal BUY decision from a screener row, with kill conditions drafted from the section 8 template."""
    from desk.decisions import reasoning_markdown
    from desk.portfolio import Limits
    from desk.rules import RuleConfig

    settings = settings or get_settings()
    today = today or dt.date.today()
    inst = session.get(Instrument, instrument_id)
    row = session.exec(
        select(ScreenerRow)
        .where(ScreenerRow.instrument_id == instrument_id)
        .order_by(ScreenerRow.date.desc())
        .limit(1)
    ).first()
    if row is None:
        raise ValueError("no screener row for this instrument")
    existing = session.exec(
        select(Decision).where(
            Decision.instrument_id == instrument_id,
            Decision.date == today,
            Decision.action == "BUY",
        )
    ).first()
    if existing is not None:
        return existing
    view = build_portfolio(session, settings)
    limits = Limits.load(settings.config_dir / "limits.yaml")
    cfg = RuleConfig.load(settings)
    theme_w = view.by_theme.get(inst.theme or "", 0.0)
    cash = view.cash_eur / view.total_eur if view.total_eur else 0.0
    size = max(0.0, min(limits.max_single_position, limits.max_single_theme - theme_w, 0.05, cash))
    stop = cfg.stop_loss.get(inst.kind.value) or 0.18
    kill = {
        "thesis": f"Screener candidate on {row.date}: rank {row.rank}, score {row.total:.0f}. Quality gates passed. "
        f"Thesis to be written by the user before execution.",
        "kills": [
            {
                "predicate": f"close('{inst.ticker}') < {1 - stop:.2f} * avg_cost('{inst.ticker}')",
                "severity": "mandatory",
                "note": f"{stop:.0%} stop from average cost (section 8 default for {inst.kind.value})",
            },
            {
                "predicate": f"house_view('sector', '{inst.sector}').stance == 'least_preferred'",
                "severity": "mandatory",
                "note": "Safra moves the sector to least preferred",
            }
            if inst.sector
            else None,
            {
                "human": "Score falls below 45 on two consecutive runs, or a quality gate fails at the weekly refresh",
                "severity": "review",
            },
        ],
        "add_blocked_while": None,
        "pre_condition": None,
        "theme": inst.theme,
        "tradable": inst.tradable,
    }
    kill["kills"] = [k for k in kill["kills"] if k]
    regime = latest_regime(session) or Regime(
        date=today,
        label="regime unknown",
        inflation_state="",
        policy_state="",
        oil_state="",
        vol_state="",
    )
    from desk.models import Score
    from desk.score import Factor, ScoreResult, band

    factors = {
        k: Factor(v.get("value"), v.get("inputs") or {})
        for k, v in ((row.factors_json or {}).get("factors") or {}).items()
    }
    score_row = Score(
        instrument_id=inst.id,
        date=row.date,
        total=row.total,
        f_safra=factors.get("safra", Factor(0)).effective,
        f_regime=factors.get("regime", Factor(0)).effective,
        f_portfolio=factors.get("portfolio", Factor(0)).effective,
        f_valuation=factors.get("valuation", Factor(0)).effective,
        f_momentum=factors.get("momentum", Factor(0)).effective,
        f_season=factors.get("season", Factor(0)).effective,
        f_crowd=factors.get("crowd", Factor(0)).effective,
        inputs_json=row.factors_json,
    )
    session.add(score_row)
    session.commit()
    session.refresh(score_row)
    res = ScoreResult(score_row, factors, False, list((row.factors_json or {}).get("notes") or []))
    ref = next((p.price for p in view.positions if p.instrument.id == inst.id), None)
    if ref is None:
        from desk.score import _latest_price

        px = _latest_price(session, inst.id)
        ref = px.close if px else None
    d = Decision(
        date=today,
        instrument_id=inst.id,
        action="BUY",
        size_pct=size,
        score_id=score_row.id,
        rules_json={
            "flags": [],
            "kill_condition": kill["thesis"],
            "kill_predicate": kill["kills"][0]["predicate"],
            "kill_json": kill,
            "score": row.total,
            "band": band(row.total),
            "provisional": False,
            "basis": view.basis,
            "reference_price": ref,
            "reference_date": str(today),
            "source": "screener",
            "screener": {"date": row.date.isoformat(), "rank": row.rank, "gates": row.gates_json},
        },
        reasoning_md=reasoning_markdown(
            "BUY",
            inst,
            regime,
            res,
            [],
            size,
            kill,
            "Score below 45, a failed quality gate at the weekly refresh, or the kill conditions above.",
            f"Proposed from the screener (rank {row.rank} on {row.date}); the thesis must be written before execution.",
        ),
        created_at=utcnow(),
    )
    session.add(d)
    session.commit()
    session.refresh(d)
    return d
