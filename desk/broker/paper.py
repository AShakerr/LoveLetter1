"""PaperBroker (docs/BRIEF.md 8b). Fills every MARKET order at the next session's open from `prices`, applies the
spread from config/costs.yaml plus a flat venue fee, fills LIMIT orders when the next session's range crosses the
limit (else expires at DAY), and records slippage against the decision's reference close. Paper cash and
positions live in `positions` with broker="paper"."""

from __future__ import annotations

import datetime as dt
import logging
import re
from decimal import Decimal

from sqlmodel import Session, select

from desk.broker.base import Costs, Fill, Order
from desk.config import Settings, get_settings
from desk.models import FillRow, Instrument, InstrumentKind, OrderRow, Position, Pot, Price
from desk.sources.base import utcnow

log = logging.getLogger(__name__)

PAPER = "paper"


def _ref_date(client_ref: str) -> dt.date:
    m = re.search(r"\d{4}-\d{2}-\d{2}", client_ref)
    return dt.date.fromisoformat(m.group(0)) if m else dt.date.today()


class PaperBroker:
    name = PAPER
    mode = "paper"

    def __init__(
        self, session: Session, settings: Settings | None = None, costs: Costs | None = None
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()
        self.costs = costs or Costs.load(self.settings.config_dir / "costs.yaml")

    # -- book -------------------------------------------------------------------------------------
    def positions(self) -> list[Position]:
        return list(
            self.session.exec(
                select(Position).where(Position.broker == PAPER, Position.closed_at.is_(None))
            ).all()
        )

    def cash(self) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        for p in self.positions():
            inst = self.session.get(Instrument, p.instrument_id)
            if inst.kind == InstrumentKind.cash:
                out[p.currency] = out.get(p.currency, Decimal(0)) + Decimal(str(p.quantity))
        return out

    def is_tradable(self, instrument: Instrument) -> bool:
        return bool(instrument.tradable)

    def is_seeded(self) -> bool:
        return (
            self.session.exec(select(Position).where(Position.broker == PAPER).limit(1)).first()
            is not None
        )

    def seed_from_actual(self, force: bool = False) -> int:
        """Copy the confirmed manual book into the paper book so the two diverge only through decisions."""
        if self.is_seeded() and not force:
            return 0
        for row in self.session.exec(select(Position).where(Position.broker == PAPER)).all():
            self.session.delete(row)
        rows = self.session.exec(
            select(Position).where(
                Position.broker == "manual",
                Position.confirmed_by_user.is_(True),
                Position.closed_at.is_(None),
            )
        ).all()
        n = 0
        for p in rows:
            self.session.add(
                Position(
                    instrument_id=p.instrument_id,
                    quantity=p.quantity,
                    avg_cost=p.avg_cost,
                    currency=p.currency,
                    pot=p.pot,
                    as_of=p.as_of,
                    confirmed_by_user=True,
                    last_price=p.last_price,
                    value_native=p.value_native,
                    return_pct=p.return_pct,
                    source="paper",
                    batch="paper:seed",
                    broker=PAPER,
                    kill_condition=p.kill_condition,
                    kill_predicate=p.kill_predicate,
                    kill_json=p.kill_json,
                    stop_pct=p.stop_pct,
                )
            )
            n += 1
        self.session.commit()
        return n

    # -- orders -------------------------------------------------------------------------------------
    def submit(self, order: Order) -> str:
        """Record the order as submitted; the fill happens at the next session's open (see settle)."""
        existing = self.session.exec(
            select(OrderRow).where(OrderRow.client_ref == order.client_ref)
        ).first()
        if existing is not None:
            return existing.broker_order_id or existing.client_ref
        inst = self.session.get(Instrument, order.instrument_id)
        row = OrderRow(
            decision_id=order.decision_id,
            instrument_id=order.instrument_id,
            broker=PAPER,
            broker_order_id=order.client_ref,
            client_ref=order.client_ref,
            side=order.side,
            quantity=float(order.quantity) if order.quantity is not None else None,
            notional=float(order.notional) if order.notional is not None else None,
            currency=inst.currency,
            order_type=order.order_type,
            limit_price=float(order.limit_price) if order.limit_price is not None else None,
            time_in_force=order.time_in_force,
            status="submitted",
            submitted_at=utcnow(),
            order_date=_ref_date(order.client_ref),
            created_at=utcnow(),
        )
        self.session.add(row)
        self.session.commit()
        return row.client_ref

    def cancel(self, order_id: str) -> None:
        row = self.session.exec(select(OrderRow).where(OrderRow.client_ref == order_id)).first()
        if row is not None and row.status in ("pending", "submitted"):
            row.status = "cancelled"
            self.session.add(row)
            self.session.commit()

    def fills(self, since: dt.datetime) -> list[Fill]:
        rows = self.session.exec(
            select(FillRow, OrderRow)
            .join(OrderRow, FillRow.order_id == OrderRow.id)
            .where(OrderRow.broker == PAPER, FillRow.filled_at >= since)
        ).all()
        return [
            Fill(
                order_id=o.client_ref,
                filled_at=f.filled_at,
                quantity=Decimal(str(f.quantity)),
                price=Decimal(str(f.price)),
                fees=Decimal(str(f.fees)),
                currency=f.currency,
                slippage_bps=None if f.slippage_bps is None else Decimal(str(f.slippage_bps)),
            )
            for f, o in rows
        ]

    # -- settlement ---------------------------------------------------------------------------------
    def _next_session(self, instrument_id: int, after: dt.date) -> Price | None:
        return self.session.exec(
            select(Price)
            .where(Price.instrument_id == instrument_id, Price.date > after)
            .order_by(Price.date)
            .limit(1)
        ).first()

    def settle(self, market_cap: dict[int, float] | None = None) -> list[FillRow]:
        """Fill every submitted paper order whose next session exists in `prices`."""
        market_cap = market_cap or {}
        done: list[FillRow] = []
        for order in self.session.exec(
            select(OrderRow).where(OrderRow.broker == PAPER, OrderRow.status == "submitted")
        ).all():
            inst = self.session.get(Instrument, order.instrument_id)
            px = self._next_session(inst.id, order.order_date)
            if px is None:
                continue
            base = px.open if px.open else px.close
            spread = self.costs.spread_for(inst, market_cap.get(inst.id)) / 1e4
            if order.order_type == "LIMIT":
                lim = order.limit_price or 0.0
                lo, hi = (
                    (px.low if px.low is not None else base),
                    (px.high if px.high is not None else base),
                )
                if order.side == "BUY" and lo <= lim:
                    price = min(base, lim)
                elif order.side == "SELL" and hi >= lim:
                    price = max(base, lim)
                else:
                    if order.time_in_force == "DAY":
                        order.status, order.error = (
                            "cancelled",
                            f"limit {lim} not reached on {px.date} (DAY expired)",
                        )
                        self.session.add(order)
                    continue
            else:
                price = base * (1 + spread) if order.side == "BUY" else base * (1 - spread)
            qty = order.quantity if order.quantity is not None else (order.notional or 0.0) / price
            fees = self.costs.fee_for(inst)
            slippage = None
            if order.reference_price:
                raw = price / order.reference_price - 1
                slippage = (raw if order.side == "BUY" else -raw) * 1e4
            fill = FillRow(
                order_id=order.id,
                filled_at=dt.datetime.combine(px.date, dt.time(9, 0)),
                quantity=qty,
                price=price,
                fees=fees,
                currency=inst.currency,
                slippage_bps=slippage,
                note=f"{order.side} at {px.date} open {base:.4f} {'+' if order.side == 'BUY' else '-'}{spread * 1e4:.0f}bps"
                f" ({self.costs.instrument_class(inst, market_cap.get(inst.id))})",
            )
            self.session.add(fill)
            order.status = "filled"
            self.session.add(order)
            self._apply(inst, order.side, qty, price, fees, px.date)
            done.append(fill)
        self.session.commit()
        return done

    # -- book updates through fills only --------------------------------------------------------------
    def _cash_row(self, ccy: str) -> Position:
        ticker = f"CASH_{ccy}"
        inst = self.session.exec(select(Instrument).where(Instrument.ticker == ticker)).first()
        if inst is None:
            inst = Instrument(
                ticker=ticker,
                name=f"{ccy} cash",
                kind=InstrumentKind.cash,
                currency=ccy,
                tradable=False,
                theme="cash",
            )
            self.session.add(inst)
            self.session.commit()
            self.session.refresh(inst)
        row = self.session.exec(
            select(Position).where(
                Position.broker == PAPER,
                Position.instrument_id == inst.id,
                Position.closed_at.is_(None),
            )
        ).first()
        if row is None:
            row = Position(
                instrument_id=inst.id,
                quantity=0.0,
                avg_cost=1.0,
                currency=ccy,
                pot=Pot.brokerage,
                as_of=dt.date.today(),
                confirmed_by_user=True,
                source="paper",
                batch="paper:cash",
                broker=PAPER,
            )
            self.session.add(row)
        return row

    def _fx_to(self, from_ccy: str, to_ccy: str, on: dt.date) -> float:
        """Units of to_ccy per unit of from_ccy using EUR<ccy>=X closes; 1.0 when unknown."""
        if from_ccy == to_ccy:
            return 1.0

        def per_eur(ccy: str) -> float | None:
            if ccy == "EUR":
                return 1.0
            pair = self.session.exec(
                select(Instrument).where(Instrument.ticker == f"EUR{ccy}=X")
            ).first()
            if pair is None:
                return None
            px = self.session.exec(
                select(Price)
                .where(Price.instrument_id == pair.id, Price.date <= on)
                .order_by(Price.date.desc())
                .limit(1)
            ).first()
            return px.close if px else None

        a, b = per_eur(from_ccy), per_eur(to_ccy)
        return (b / a) if a and b else 1.0

    def _apply(
        self, inst: Instrument, side: str, qty: float, price: float, fees: float, on: dt.date
    ) -> None:
        pos = self.session.exec(
            select(Position).where(
                Position.broker == PAPER,
                Position.instrument_id == inst.id,
                Position.closed_at.is_(None),
            )
        ).first()
        cost = qty * price + fees
        if side == "BUY":
            if pos is None:
                pos = Position(
                    instrument_id=inst.id,
                    quantity=0.0,
                    avg_cost=price,
                    currency=inst.currency,
                    pot=Pot.brokerage,
                    as_of=on,
                    confirmed_by_user=True,
                    source="paper",
                    batch="paper:fills",
                    broker=PAPER,
                )
            total_cost = pos.avg_cost * pos.quantity + price * qty
            pos.quantity += qty
            pos.avg_cost = total_cost / pos.quantity if pos.quantity else price
            pos.last_price, pos.as_of = price, on
            self.session.add(pos)
            self._debit(inst.currency, cost, on)
        else:
            if pos is None:
                log.warning("paper SELL %s with no paper position", inst.ticker)
                return
            pos.quantity = max(0.0, pos.quantity - qty)
            pos.last_price, pos.as_of = price, on
            if pos.quantity <= 1e-9:
                pos.closed_at = utcnow()
            self.session.add(pos)
            self._credit(inst.currency, qty * price - fees)

    def _credit(self, ccy: str, amount: float) -> None:
        row = self._cash_row(ccy)
        row.quantity += amount
        self.session.add(row)

    def _debit(self, ccy: str, amount: float, on: dt.date) -> None:
        """Draw from same-currency cash first, then convert the shortfall from other cash rows at the day's FX."""
        row = self._cash_row(ccy)
        take = min(max(row.quantity, 0.0), amount)
        row.quantity -= take
        self.session.add(row)
        short = amount - take
        if short <= 1e-9:
            return
        for other in self.positions():
            oi = self.session.get(Instrument, other.instrument_id)
            if oi.kind != InstrumentKind.cash or other.currency == ccy or other.quantity <= 0:
                continue
            rate = self._fx_to(other.currency, ccy, on)  # ccy per other
            avail_in_ccy = other.quantity * rate
            use = min(avail_in_ccy, short)
            other.quantity -= use / rate
            self.session.add(other)
            short -= use
            if short <= 1e-9:
                break
        if short > 1e-9:  # still short: the paper book goes negative and says so
            row.quantity -= short
            self.session.add(row)
            log.warning("paper cash %s negative by %.2f after fill", ccy, short)
