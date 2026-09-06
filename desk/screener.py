"""Daily screener (docs/BRIEF.md 8c): rank a defined universe by the same conviction score, apply the quality and
value-trap gates, keep the top and bottom 15. Nothing here is a decision until "Propose BUY" is pressed."""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass, field
from typing import Any

import yaml
from sqlmodel import Session, select

from desk.config import Settings, get_settings
from desk.flow import flow_badge
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
from desk.score import ScoreResult, ValuationContext, compute_score, three_month_return
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
    max_per_sector: int = 5

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
            "max_per_sector",
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


# Yahoo exchange suffix and quote currency by country (STOXX 600 table) or by exchange name.
COUNTRY_SUFFIX = {
    "switzerland": (".SW", "CHF"),
    "united kingdom": (".L", "GBP"),
    "uk": (".L", "GBP"),
    "britain": (".L", "GBP"),
    "germany": (".DE", "EUR"),
    "france": (".PA", "EUR"),
    "netherlands": (".AS", "EUR"),
    "sweden": (".ST", "SEK"),
    "denmark": (".CO", "DKK"),
    "finland": (".HE", "EUR"),
    "italy": (".MI", "EUR"),
    "spain": (".MC", "EUR"),
    "norway": (".OL", "NOK"),
    "belgium": (".BR", "EUR"),
    "portugal": (".LS", "EUR"),
    "austria": (".VI", "EUR"),
    "ireland": (".IR", "EUR"),
    "poland": (".WA", "PLN"),
    "luxembourg": (".PA", "EUR"),
    "czech republic": (".PR", "CZK"),
    "czechia": (".PR", "CZK"),
    "greece": (".AT", "EUR"),
    "jersey": (".L", "GBP"),
    "guernsey": (".L", "GBP"),
    "isle of man": (".L", "GBP"),
}
EXCHANGE_SUFFIX = {
    "six": ".SW",
    "london": ".L",
    "xetra": ".DE",
    "frankfurt": ".DE",
    "euronext paris": ".PA",
    "paris": ".PA",
    "euronext amsterdam": ".AS",
    "amsterdam": ".AS",
    "stockholm": ".ST",
    "nasdaq stockholm": ".ST",
    "copenhagen": ".CO",
    "helsinki": ".HE",
    "milan": ".MI",
    "borsa italiana": ".MI",
    "madrid": ".MC",
    "oslo": ".OL",
    "brussels": ".BR",
    "lisbon": ".LS",
    "vienna": ".VI",
    "dublin": ".IR",
    "warsaw": ".WA",
    "prague": ".PR",
    "athens": ".AT",
}


def european_yahoo_symbol(
    ticker: str, country: str | None, exchange: str | None = None
) -> tuple[str | None, str]:
    """(yahoo symbol, quote currency) for a STOXX 600 ticker: 'AMBU B' + Denmark -> ('AMBU-B.CO', 'DKK').
    None when the venue is unknown; the name is then priced only if a source_symbol is set by hand."""
    sym = ticker.strip().upper().replace(" ", "-").replace(".", "-")
    if country:
        hit = COUNTRY_SUFFIX.get(country.strip().lower())
        if hit:
            return sym + hit[0], hit[1]
    if exchange:
        ex = exchange.strip().lower()
        for key, suffix in EXCHANGE_SUFFIX.items():
            if key in ex:
                ccy = next((c for s, c in COUNTRY_SUFFIX.values() if s == suffix), "EUR")
                return sym + suffix, ccy
    return None, "EUR"


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
        ccol = next(
            (c for c in df.columns if any(k in str(c) for k in ("Country", "Domicile", "Nation"))),
            None,
        )
        xcol = next((c for c in df.columns if "Exchange" in str(c) or "Market" in str(c)), None)
        if ccol is None and xcol is None:
            log.warning(
                "stoxx600: no country/exchange column in the Wikipedia table (%s); "
                "European names stay unpriced until the table carries one",
                [str(c) for c in df.columns],
            )
        unmapped: dict[str, int] = {}
        for _, r in df.iterrows():
            sym = str(r.get(tcol, "")).strip()
            if not sym or sym == "nan":
                continue
            country = str(r.get(ccol, "")).strip() if ccol else None
            exchange = str(r.get(xcol, "")).strip() if xcol else None
            yahoo, ccy = european_yahoo_symbol(sym, country or None, exchange or None)
            if yahoo is None:
                key = country or exchange or "(no country/exchange value)"
                unmapped[key] = unmapped.get(key, 0) + 1
            out.append(
                {
                    "ticker": sym.upper(),
                    "name": str(r.get(ncol, sym)),
                    "sector": str(r.get(scol, "")) if scol else "",
                    "exchange": exchange or "Europe",
                    "region": "Euro area",
                    "currency": ccy,
                    "source_symbol": yahoo,
                }
            )
        if unmapped:
            top = sorted(unmapped.items(), key=lambda kv: -kv[1])[:8]
            log.warning(
                "stoxx600: %d of %d rows have no venue mapping; unmapped values: %s "
                "(columns seen: %s)",
                sum(unmapped.values()),
                len(out),
                ", ".join(f"{k!r} x{n}" for k, n in top),
                [str(c) for c in df.columns],
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


_NOISE = {
    "inc",
    "inc.",
    "plc",
    "sa",
    "se",
    "ag",
    "nv",
    "n.v.",
    "co",
    "co.",
    "corp",
    "corporation",
    "ltd",
    "limited",
    "the",
    "group",
    "holdings",
    "holding",
    "company",
    "&",
    "and",
    "spa",
    "ab",
    "asa",
    "oyj",
}


def _same_company(a: str | None, b: str | None) -> bool:
    """Loose name match for ticker collisions: the first two meaningful words agree."""

    def words(s: str | None) -> list[str]:
        return [w for w in re.sub(r"[^a-z0-9 ]", " ", (s or "").lower()).split() if w not in _NOISE]

    wa, wb = words(a), words(b)
    if not wa or not wb:
        return False
    return wa[:2] == wb[:2] or wa[0] == wb[0] and (len(wa) == 1 or len(wb) == 1)


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
        collisions: list[dict[str, str]] = []
        for r in rows:
            t = r["ticker"]
            inst = session.exec(select(Instrument).where(Instrument.ticker == t)).first()
            if inst is not None and inst.screener_member and inst.screener_member != source:
                # the same ticker on another list: Linde is LIN on the NYSE and LIN in the STOXX table
                if _same_company(inst.name, r.get("name")):
                    collisions.append(
                        {
                            "ticker": t,
                            "kept": inst.screener_member,
                            "reason": "same company; one line kept",
                        }
                    )
                    seen.add(t)
                    continue
                # a different company: keep both under distinct tickers (the venue symbol names the second)
                alt = r.get("source_symbol") or f"{t}:{source}"
                collisions.append({"ticker": t, "kept": inst.screener_member, "renamed": alt})
                r = dict(r, ticker=alt)
                t = alt
                inst = session.exec(select(Instrument).where(Instrument.ticker == t)).first()
            seen.add(t)
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
                # venue symbol, quote currency and exchange follow the table (a bare STOXX ticker is useless)
                for field_name in ("source_symbol", "currency", "exchange"):
                    new_v = r.get(field_name)
                    if new_v and getattr(inst, field_name) != new_v:
                        setattr(inst, field_name, new_v)
                        changed = True
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
        resolved = [r for r in rows if r.get("source_symbol")]
        entry: dict[str, Any] = {
            "status": "ok",
            "members": len(seen),
            "added": added,
            "updated": updated,
            "dropped": dropped,
            "symbols_resolved": len(resolved),
            "symbols_unresolved": len(rows) - len(resolved),
            "sample": [f"{r['ticker']} -> {r['source_symbol']}" for r in resolved[:10]],
        }
        if collisions:
            entry["collisions"] = collisions
        if rows and not resolved and source != "safra_focus_list":
            entry["warning"] = (
                f"{source}: no row resolved to a venue symbol; these names cannot be priced. "
                "The Wikipedia table's country/exchange column did not map (see the log)."
            )
            log.warning(entry["warning"])
        summary[source] = entry
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
        has_fundamentals = any(v is not None for v in f.values())
        passed, reasons = quality_gate(f, inst.sector) if has_fundamentals else (False, [])
        if not has_fundamentals:
            reasons = ["no fundamentals (weekly job has not populated this name)"]
        gates = {
            "fundamentals": has_fundamentals,
            "quality": passed,
            "quality_reasons": reasons,
            "value_trap": any("value trap" in x for x in res.flags),
            "no_earnings": any("no earnings" in x for x in res.flags),
            "fundamentals_as_of": vctx.as_of(inst.id).get("date"),
        }
        gates["passed"] = passed and not gates["value_trap"] and not gates["no_earnings"]
        gates["reason"] = gate_reason(gates)
        scored.append((inst, res, gates))
    scored.sort(key=rank_key)
    held = {p.instrument.id for p in view.positions}
    rank_of = {inst.id: i + 1 for i, (inst, _, _) in enumerate(scored)}
    tiebreaks = tiebreak_notes(scored)
    top, overflow, excluded = select_top(scored, cfg.top_n, cfg.max_per_sector)
    bottom = scored[-cfg.top_n :] if len(scored) > cfg.top_n else []
    lists: dict[int, str] = {}
    for inst, _, _ in top:
        lists[inst.id] = "top"
    for inst, _, _ in overflow:
        lists.setdefault(inst.id, "overflow")
    for inst, _, _ in bottom:
        lists.setdefault(inst.id, "bottom")
    for inst, _res, _gates in scored[: cfg.sentiment_calls_per_day]:
        # kept so tomorrow's Alpha Vantage calls know the top 20
        lists.setdefault(inst.id, "sentiment")
    for inst, res, _gates in scored:
        if inst.id in held and res.row.total < 45:
            lists.setdefault(inst.id, "held_avoid")
    keep = {inst.id: (inst, res, gates) for inst, res, gates in scored if inst.id in lists}
    for old in session.exec(select(ScreenerRow).where(ScreenerRow.date == today)).all():
        session.delete(old)
    session.commit()
    list_rank = {inst.id: i + 1 for i, (inst, _, _) in enumerate(top)}
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
                    "list": lists[inst.id],
                    "list_rank": list_rank.get(inst.id),
                    "excluded": excluded.get(inst.id),
                    "tiebreak": tiebreaks.get(inst.id),
                    "sentiment": sentiment_source(session, inst, today),
                    "fundamentals": {k: v[0] for k, v in vctx.latest(inst.id).items()},
                    "safra": _safra_view(session, inst),
                },
                gates_json=gates,
            )
        )
    session.commit()
    sectors: dict[str, int] = {}
    for inst, _, _ in top:
        sectors[inst.sector or "—"] = sectors.get(inst.sector or "—", 0) + 1
    return {
        "date": today.isoformat(),
        "scored": len(scored),
        "written": len(keep),
        "top": [(i.ticker, r.row.total, g["passed"]) for i, r, g in top],
        "sectors": sectors,
        "overflow": [(i.ticker, r.row.total, excluded.get(i.id)) for i, r, _ in overflow],
    }


def gate_reason(gates: dict[str, Any]) -> str | None:
    """Text for the badge: None when passed, else what gated the name out."""
    if gates.get("passed"):
        return None
    reasons = list(gates.get("quality_reasons") or [])
    if gates.get("no_earnings"):
        reasons.append("no earnings")
    if gates.get("value_trap"):
        reasons.append("possible value trap")
    return "; ".join(reasons) or "gated"


def rank_key(t: tuple[Instrument, ScoreResult, dict[str, Any]]):
    """Descending total; ties on the displayed integer are broken by valuation, then crowd, then the raw total."""
    _inst, res, _gates = t
    return (
        -round(res.row.total),
        -(res.factors["valuation"].effective),
        -(res.factors["crowd"].effective),
        -res.row.total,
    )


def tiebreak_notes(scored: list[tuple[Instrument, ScoreResult, dict[str, Any]]]) -> dict[int, str]:
    """For a name that shares its displayed score with the one ranked just above it, say what separated them."""
    out: dict[int, str] = {}
    for prev, cur in zip(scored, scored[1:], strict=False):
        if round(prev[1].row.total) != round(cur[1].row.total):
            continue
        pv, cv = prev[1].factors["valuation"].effective, cur[1].factors["valuation"].effective
        pc, cc = prev[1].factors["crowd"].effective, cur[1].factors["crowd"].effective
        shown = round(cur[1].row.total)
        if pv != cv:
            out[cur[0].id] = (
                f"tie at {shown}: valuation {cv:.2f} vs {pv:.2f} ({prev[0].ticker}) above"
            )
        elif pc != cc:
            out[cur[0].id] = (
                f"tie at {shown}: valuation equal, crowd {cc:.2f} vs {pc:.2f} ({prev[0].ticker}) above"
            )
        else:
            out[cur[0].id] = (
                f"tie at {shown}: raw total {cur[1].row.total:.2f} vs {prev[1].row.total:.2f} ({prev[0].ticker})"
            )
    return out


def select_top(
    scored: list[tuple[Instrument, ScoreResult, dict[str, Any]]], top_n: int, max_per_sector: int
) -> tuple[list, list, dict[int, str]]:
    """The candidate list: the highest-ranked names that pass the gates, at most `max_per_sector` per sector.
    Overflow = names ranked above the last candidate that were skipped, with the reason."""
    top: list = []
    overflow: list = []
    excluded: dict[int, str] = {}
    per_sector: dict[str, int] = {}
    for inst, res, gates in scored:
        if len(top) >= top_n:
            break
        sector = inst.sector or "—"
        if not gates.get("passed"):
            excluded[inst.id] = f"gated out: {gates.get('reason') or 'gates failed'}"
            overflow.append((inst, res, gates))
            continue
        if per_sector.get(sector, 0) >= max_per_sector:
            excluded[inst.id] = (
                f"sector cap: {sector} already has {max_per_sector} in the top {top_n}"
            )
            overflow.append((inst, res, gates))
            continue
        per_sector[sector] = per_sector.get(sector, 0) + 1
        top.append((inst, res, gates))
    return top, overflow, excluded


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
    session: Session,
    settings: Settings | None = None,
    today: dt.date | None = None,
    budget: int | None = None,
) -> list[str]:
    """Tickers that get Alpha Vantage calls today, in budget order (brief 8c): held single names first, then
    the screener's top N by pre-sentiment score from the most recent run (yesterday's, since the screener runs
    after the fetch). `budget` caps the list; topics are budgeted separately by the fetcher."""
    settings = settings or get_settings()
    today = today or dt.date.today()
    cfg = ScreenerConfig.load(settings)
    held = [
        session.get(Instrument, p.instrument_id)
        for p in session.exec(
            select(Position).where(
                Position.confirmed_by_user.is_(True),
                Position.closed_at.is_(None),
                Position.broker == "manual",
            )
        ).all()
    ]
    held_tickers = [
        i.ticker
        for i in held
        if i is not None
        and i.kind == InstrumentKind.stock
        and (i.region == "USA" or i.screener_member)
    ]
    latest = session.exec(
        select(ScreenerRow).where(ScreenerRow.date <= today).order_by(ScreenerRow.date.desc())
    ).first()
    top: list[str] = []
    if latest is not None:
        rows = session.exec(
            select(ScreenerRow).where(ScreenerRow.date == latest.date).order_by(ScreenerRow.rank)
        ).all()
        top = [
            session.get(Instrument, r.instrument_id).ticker
            for r in rows
            if r.rank <= cfg.sentiment_calls_per_day
        ]
    out: list[str] = []
    for t in held_tickers + top:
        if t not in out:
            out.append(t)
    return out[:budget] if budget is not None else out


# ------------------------------------------------------------------------------------------- read model
def days_in_top(
    session: Session, instrument_id: int, today: dt.date, top_n: int, lookback: int = 10
) -> int:
    """Consecutive trading days (screener runs) the name has been in the candidate list, ending today."""
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
        if row is None:
            break
        if (
            (row.factors_json or {}).get("list") not in (None, "top")
            or row.rank > top_n
            and not (row.factors_json or {}).get("list_rank")
        ):
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
    out_c, out_o, out_a = [], [], []
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
            "flow": flow_badge(session, inst.id, day),
            "factors": fj.get("factors") or {},
            "notes": fj.get("notes") or [],
            "flags": fj.get("flags") or [],
            "gates": r.gates_json or {},
            "sentiment": fj.get("sentiment") or {},
            "fundamentals": fj.get("fundamentals") or {},
            "safra": fj.get("safra"),
            "held": fj.get("held"),
            "list": fj.get("list"),
            "list_rank": fj.get("list_rank"),
            "excluded": fj.get("excluded"),
            "tiebreak": fj.get("tiebreak"),
            "streak": streak,
            "can_propose": r.total >= 75
            and port_fit >= 3
            and fj.get("list") == "top"
            and (r.gates_json or {}).get("passed")
            and streak >= cfg.anti_churn_days
            and proposed is None
            and inst.tradable,
            "proposed": proposed,
        }
        which = fj.get("list")
        if which == "top":
            out_c.append(item)
        elif which == "overflow":
            out_o.append(item)
        elif which in ("bottom", "held_avoid"):
            out_a.append(item)
        # "sentiment" rows exist only to steer tomorrow's Alpha Vantage calls
    out_c.sort(key=lambda it: it["list_rank"] or 0)
    sectors: dict[str, int] = {}
    for it in out_c:
        sectors[it["inst"].sector or "—"] = sectors.get(it["inst"].sector or "—", 0) + 1
    return {
        "date": day,
        "candidates": out_c,
        "overflow": out_o,
        "avoid": out_a,
        "sectors": sectors,
        "cfg": cfg,
    }


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
    from desk.kill_conditions import draft_kill_conditions
    from desk.score import _latest_price

    px = _latest_price(session, inst.id)
    kill = draft_kill_conditions(session, inst, px.close if px else None, stop, settings)
    kill["thesis"] = (
        f"Screener candidate on {row.date}: rank {row.rank}, score {row.total:.0f}. Quality gates passed. "
        + kill["thesis"]
    )
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


def universe_report(
    session: Session, largest: int = 0, source: str | None = None
) -> dict[str, Any]:
    """Counts per constituent source and, optionally, the largest names by market cap from stored fundamentals."""
    import datetime as dt

    from sqlalchemy import func

    from desk.models import Fundamental, Price

    members = session.exec(select(Instrument).where(Instrument.screener_member.is_not(None))).all()
    since = dt.date.today() - dt.timedelta(days=7)
    priced = {
        r[0]
        for r in session.exec(
            select(Price.instrument_id).where(Price.date >= since).group_by(Price.instrument_id)
        ).all()
    }
    caps = {
        r[0]: r[1]
        for r in session.exec(
            select(Fundamental.instrument_id, func.max(Fundamental.value))
            .where(Fundamental.field == "marketCap")
            .group_by(Fundamental.instrument_id)
        ).all()
    }
    with_f = {
        r[0]
        for r in session.exec(
            select(Fundamental.instrument_id).group_by(Fundamental.instrument_id)
        ).all()
    }
    out: dict[str, Any] = {"sources": {}}
    for src in sorted({m.screener_member for m in members}):
        rows = [m for m in members if m.screener_member == src]
        active = [m for m in rows if not m.screener_dropped]
        out["sources"][src] = {
            "members": len(rows),
            "dropped": len(rows) - len(active),
            "tradable": sum(1 for m in active if m.tradable),
            "entering_screener_now": sum(1 for m in active if m.tradable),
            "would_enter_if_all_tradable": len(active),
            "with_venue_symbol": sum(1 for m in active if m.source_symbol),
            "priced_last_7d": sum(1 for m in active if m.id in priced),
            "with_fundamentals": sum(1 for m in active if m.id in with_f),
        }
    if largest:
        pool = [
            m
            for m in members
            if not m.screener_dropped and (not source or m.screener_member == source)
        ]
        ranked = sorted(pool, key=lambda m: -(caps.get(m.id) or 0))
        out["largest"] = [
            {
                "ticker": m.ticker,
                "yahoo": m.source_symbol,
                "name": m.name,
                "sector": m.sector,
                "market_cap": caps.get(m.id),
                "tradable": m.tradable,
            }
            for m in ranked[:largest]
        ]
        out["largest_note"] = (
            f"{sum(1 for m in pool if caps.get(m.id))} of {len(pool)} names have a stored marketCap; "
            "run `desk fundamentals` first if the count is low"
        )
    return out
