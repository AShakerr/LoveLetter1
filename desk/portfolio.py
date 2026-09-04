"""Read model for the portfolio page: EUR valuation, theme and FX exposure, limit bars.

Valuation: latest close from `prices` when one exists, else the price the snapshot recorded
(`positions.last_price`). Native value is converted to EUR with the latest EURUSD=X / EURGBP=X close."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from sqlmodel import Session, select

from desk.config import Settings, get_settings
from desk.models import Instrument, InstrumentKind, Position, Price


@dataclass
class Limits:
    max_single_position: float = 0.15
    max_single_theme: float = 0.35
    max_illiquid_private: float = 0.15
    min_diversified_core_target: float = 0.40
    min_diversified_core_warn: float = 0.25
    max_crypto: float = 0.05

    @classmethod
    def load(cls, path: Path) -> Limits:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls(**{k: float(v) for k, v in doc.items() if k in cls.__dataclass_fields__})


@dataclass
class FxRate:
    ccy: str
    per_eur: float | None  # units of ccy per 1 EUR
    as_of: dt.date | None
    source: str | None


@dataclass
class PositionView:
    position: Position
    instrument: Instrument
    price: float | None
    price_as_of: dt.date | None
    price_source: str | None
    value_native: float | None
    value_eur: float | None
    weight: float = 0.0
    pnl_pct: float | None = None

    @property
    def theme(self) -> str:
        return self.instrument.theme or "unassigned"


@dataclass
class LimitBar:
    label: str
    value: float  # fraction of portfolio
    limit: float
    status: str  # ok | warn | breach
    kind: str = "max"  # max | min
    detail: str = ""


@dataclass
class PortfolioView:
    basis: str  # "confirmed" | "pending:<batch>" | "empty"
    as_of: dt.date | None
    positions: list[PositionView] = field(default_factory=list)
    total_eur: float = 0.0
    by_theme: dict[str, float] = field(default_factory=dict)
    by_currency: dict[str, float] = field(default_factory=dict)
    by_pot: dict[str, float] = field(default_factory=dict)
    fx: dict[str, FxRate] = field(default_factory=dict)
    limits: list[LimitBar] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def cash_eur(self) -> float:
        return sum(
            p.value_eur or 0 for p in self.positions if p.instrument.kind == InstrumentKind.cash
        )


def _latest_price(session: Session, instrument_id: int) -> Price | None:
    return session.exec(
        select(Price)
        .where(Price.instrument_id == instrument_id)
        .order_by(Price.date.desc())
        .limit(1)
    ).first()


def fx_rates(session: Session, currencies: set[str]) -> dict[str, FxRate]:
    out = {"EUR": FxRate("EUR", 1.0, None, None)}
    inst = {
        i.ticker: i
        for i in session.exec(select(Instrument).where(Instrument.kind == InstrumentKind.fx)).all()
    }
    for ccy in currencies - {"EUR"}:
        pair = inst.get(f"EUR{ccy}=X")
        price = _latest_price(session, pair.id) if pair else None
        out[ccy] = FxRate(
            ccy,
            price.close if price else None,
            price.date if price else None,
            price.source if price else None,
        )
    return out


def select_positions(session: Session) -> tuple[str, list[Position]]:
    """Confirmed open positions if any exist, else the most recent pending batch (clearly labelled)."""
    confirmed = session.exec(
        select(Position).where(Position.confirmed_by_user.is_(True), Position.closed_at.is_(None))
    ).all()
    if confirmed:
        return "confirmed", list(confirmed)
    pending = session.exec(
        select(Position)
        .where(Position.confirmed_by_user.is_(False), Position.closed_at.is_(None))
        .order_by(Position.id.desc())
    ).all()
    if not pending:
        return "empty", []
    batch = pending[0].batch
    return f"pending:{batch}", [p for p in pending if p.batch == batch]


def build_portfolio(
    session: Session, settings: Settings | None = None, limits: Limits | None = None
) -> PortfolioView:
    settings = settings or get_settings()
    limits = limits or Limits.load(settings.config_dir / "limits.yaml")
    basis, rows = select_positions(session)
    view = PortfolioView(basis=basis, as_of=max((r.as_of for r in rows), default=None))
    if not rows:
        return view
    instruments = {i.id: i for i in session.exec(select(Instrument)).all()}
    view.fx = fx_rates(session, {r.currency for r in rows})
    for r in rows:
        inst = instruments[r.instrument_id]
        px = (
            _latest_price(session, inst.id)
            if inst.kind not in (InstrumentKind.cash, InstrumentKind.other)
            else None
        )
        if px is not None:
            price, as_of, src = px.close, px.date, px.source
        else:
            price, as_of, src = r.last_price, r.as_of, r.source
        if inst.kind == InstrumentKind.cash:
            native = r.quantity
        elif price is not None:
            native = r.quantity * price
        else:
            native = r.value_native
        rate = view.fx.get(r.currency)
        eur = None
        if native is not None:
            if rate and rate.per_eur:
                eur = native / rate.per_eur
            else:
                eur = native
                view.warnings.append(f"No FX rate for {r.currency}; {inst.ticker} valued 1:1")
        pnl = None
        if price is not None and r.avg_cost:
            pnl = (price / r.avg_cost - 1) * 100
        elif r.return_pct is not None:
            pnl = r.return_pct
        view.positions.append(PositionView(r, inst, price, as_of, src, native, eur, pnl_pct=pnl))
    view.total_eur = sum(p.value_eur or 0 for p in view.positions)
    if view.total_eur > 0:
        for p in view.positions:
            p.weight = (p.value_eur or 0) / view.total_eur
            view.by_theme[p.theme] = view.by_theme.get(p.theme, 0) + p.weight
            view.by_currency[p.position.currency] = (
                view.by_currency.get(p.position.currency, 0) + p.weight
            )
            view.by_pot[p.position.pot.value] = view.by_pot.get(p.position.pot.value, 0) + p.weight
    view.positions.sort(key=lambda p: -(p.value_eur or 0))
    view.limits = limit_bars(view, limits)
    return view


def _status(value: float, limit: float, kind: str, warn_at: float | None = None) -> str:
    if kind == "max":
        if value > limit:
            return "breach"
        return "warn" if value > 0.9 * limit else "ok"
    # min
    if value < (warn_at if warn_at is not None else limit):
        return "breach"
    return "ok" if value >= limit else "warn"


def limit_bars(view: PortfolioView, limits: Limits) -> list[LimitBar]:
    non_cash = [p for p in view.positions if p.instrument.kind != InstrumentKind.cash]
    largest = max(non_cash, key=lambda p: p.weight, default=None)
    bars = []
    lw = largest.weight if largest else 0.0
    bars.append(
        LimitBar(
            "Largest single position",
            lw,
            limits.max_single_position,
            _status(lw, limits.max_single_position, "max"),
            detail=f"{largest.instrument.ticker}" if largest else "",
        )
    )
    themes = {k: v for k, v in view.by_theme.items() if k != "cash"}
    top_theme = max(themes.items(), key=lambda kv: kv[1], default=("", 0.0))
    bars.append(
        LimitBar(
            "Largest theme",
            top_theme[1],
            limits.max_single_theme,
            _status(top_theme[1], limits.max_single_theme, "max"),
            detail=top_theme[0],
        )
    )
    private = sum(p.weight for p in view.positions if p.instrument.kind == InstrumentKind.private)
    bars.append(
        LimitBar(
            "Illiquid / private",
            private,
            limits.max_illiquid_private,
            _status(private, limits.max_illiquid_private, "max"),
            detail=", ".join(
                p.instrument.ticker
                for p in view.positions
                if p.instrument.kind == InstrumentKind.private
            ),
        )
    )
    core = sum(p.weight for p in view.positions if (p.instrument.theme or "") == "diversified core")
    bars.append(
        LimitBar(
            "Diversified core",
            core,
            limits.min_diversified_core_target,
            _status(
                core, limits.min_diversified_core_target, "min", limits.min_diversified_core_warn
            ),
            kind="min",
            detail=f"warn below {limits.min_diversified_core_warn:.0%}",
        )
    )
    crypto = sum(p.weight for p in view.positions if p.instrument.kind == InstrumentKind.crypto)
    bars.append(
        LimitBar("Crypto", crypto, limits.max_crypto, _status(crypto, limits.max_crypto, "max"))
    )
    unknown = sum(p.weight for p in view.positions if p.instrument.kind == InstrumentKind.other)
    if unknown:
        view.warnings.append(
            f"{unknown:.1%} of the book is unidentified (kind=other); confirm those lines"
        )
    return bars
