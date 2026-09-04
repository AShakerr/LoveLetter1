"""Brokers. The system never places real orders (docs/BRIEF.md section 12). Two adapters exist:

- PaperBroker: executes every decision the system produces at the latest close into a paper book, seeded from
  the confirmed Revolut positions. The "Paper vs actual" panel shows where the user's book has diverged from
  what the system would have done, which is the raw material for phase 4 self-scoring.
- RevolutBroker: a stub. Revolut has no API; every method raises NotImplementedError.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sqlmodel import Session, select

from desk.models import Decision, Instrument, InstrumentKind, PaperFill, PaperPosition, Position
from desk.portfolio import PortfolioView
from desk.sources.base import utcnow

PAPER_CASH_TICKER = "CASH_USD"


@dataclass
class Fill:
    instrument: Instrument
    side: str
    quantity: float
    price: float
    currency: str


class Broker(Protocol):
    name: str

    def positions(self, session: Session) -> list[PaperPosition]: ...

    def execute(self, session: Session, decision: Decision, view: PortfolioView) -> Fill | None: ...


class RevolutBroker:
    """Live adapter placeholder. Revolut has no public trading API; the user executes manually."""

    name = "revolut"

    def positions(self, session: Session) -> list:
        raise NotImplementedError(
            "Revolut has no API; positions come from screenshots (inbox/portfolio/)"
        )

    def execute(self, session: Session, decision: Decision, view: PortfolioView) -> Fill | None:
        raise NotImplementedError(
            "Revolut has no API; execute manually and mark the decision executed"
        )


class PaperBroker:
    name = "paper"

    # -- book -------------------------------------------------------------------------------------
    def positions(self, session: Session) -> list[PaperPosition]:
        return list(session.exec(select(PaperPosition)).all())

    def _get(self, session: Session, instrument_id: int) -> PaperPosition | None:
        return session.exec(
            select(PaperPosition).where(PaperPosition.instrument_id == instrument_id)
        ).first()

    def _cash_instrument(self, session: Session) -> Instrument:
        inst = session.exec(
            select(Instrument).where(Instrument.ticker == PAPER_CASH_TICKER)
        ).first()
        if inst is None:
            inst = Instrument(
                ticker=PAPER_CASH_TICKER,
                name="USD cash",
                kind=InstrumentKind.cash,
                currency="USD",
                tradable=False,
                theme="cash",
            )
            session.add(inst)
            session.commit()
            session.refresh(inst)
        return inst

    def is_seeded(self, session: Session) -> bool:
        return session.exec(select(PaperPosition).limit(1)).first() is not None

    def seed_from_actual(self, session: Session, force: bool = False) -> int:
        """Copy the confirmed open positions into the paper book. No-op when already seeded unless force."""
        if self.is_seeded(session) and not force:
            return 0
        for row in self.positions(session):
            session.delete(row)
        rows = session.exec(
            select(Position).where(
                Position.confirmed_by_user.is_(True), Position.closed_at.is_(None)
            )
        ).all()
        n = 0
        for p in rows:
            existing = self._get(session, p.instrument_id)
            if existing is None:
                session.add(
                    PaperPosition(
                        instrument_id=p.instrument_id,
                        quantity=p.quantity,
                        avg_cost=p.avg_cost,
                        currency=p.currency,
                        updated_at=utcnow(),
                    )
                )
            else:
                existing.quantity += p.quantity
                session.add(existing)
            n += 1
        session.commit()
        return n

    def _adjust(
        self, session: Session, inst: Instrument, delta_qty: float, price: float, currency: str
    ) -> PaperPosition:
        pos = self._get(session, inst.id)
        if pos is None:
            pos = PaperPosition(
                instrument_id=inst.id,
                quantity=0.0,
                avg_cost=price,
                currency=currency,
                updated_at=utcnow(),
            )
        if delta_qty > 0:
            total_cost = pos.avg_cost * pos.quantity + price * delta_qty
            pos.quantity += delta_qty
            pos.avg_cost = total_cost / pos.quantity if pos.quantity else price
        else:
            pos.quantity = max(0.0, pos.quantity + delta_qty)
        pos.updated_at = utcnow()
        session.add(pos)
        return pos

    def _cash_adjust(self, session: Session, delta_usd: float) -> None:
        cash = self._cash_instrument(session)
        pos = self._get(session, cash.id)
        if pos is None:
            pos = PaperPosition(
                instrument_id=cash.id,
                quantity=0.0,
                avg_cost=1.0,
                currency="USD",
                updated_at=utcnow(),
            )
        pos.quantity += delta_usd
        pos.updated_at = utcnow()
        session.add(pos)

    # -- execution ----------------------------------------------------------------------------------
    def execute(self, session: Session, decision: Decision, view: PortfolioView) -> Fill | None:
        """Execute BUY/ADD/TRIM/SELL at the latest close. HOLD/AVOID produce no fill."""
        if decision.action not in ("BUY", "ADD", "TRIM", "SELL"):
            return None
        if (
            session.exec(select(PaperFill).where(PaperFill.decision_id == decision.id)).first()
            is not None
        ):
            return None
        inst = session.get(Instrument, decision.instrument_id)
        pv = next((p for p in view.positions if p.instrument.id == inst.id), None)
        price = pv.price if pv and pv.price is not None else None
        currency = pv.position.currency if pv else inst.currency
        if price is None:
            from desk.score import _latest_price

            px = _latest_price(session, inst.id)
            if px is None:
                return None
            price = px.close
        rate = view.fx.get(currency)
        per_eur = rate.per_eur if rate and rate.per_eur else 1.0
        usd_rate = view.fx.get("USD")
        usd_per_eur = usd_rate.per_eur if usd_rate and usd_rate.per_eur else 1.0
        if decision.action in ("BUY", "ADD"):
            eur_amount = (decision.size_pct or 0) * view.total_eur
            qty = eur_amount * per_eur / price
            if qty <= 0:
                return None
            self._adjust(session, inst, qty, price, currency)
            self._cash_adjust(session, -eur_amount * usd_per_eur)
            side = "buy"
        else:
            pos = self._get(session, inst.id)
            if pos is None or pos.quantity <= 0:
                return None
            if decision.action == "SELL":
                qty = pos.quantity
            else:
                eur_amount = (decision.size_pct or 0) * view.total_eur
                qty = min(pos.quantity, eur_amount * per_eur / price)
            self._adjust(session, inst, -qty, price, currency)
            self._cash_adjust(session, qty * price / per_eur * usd_per_eur)
            side = "sell"
        fill = PaperFill(
            decision_id=decision.id,
            instrument_id=inst.id,
            date=decision.date,
            side=side,
            quantity=qty,
            price=price,
            currency=currency,
            created_at=utcnow(),
            note=f"paper {side} at latest close ({decision.action} {decision.size_pct or 0:.1%})",
        )
        session.add(fill)
        session.commit()
        return Fill(inst, side, qty, price, currency)

    # -- comparison ---------------------------------------------------------------------------------
    def compare(self, session: Session, view: PortfolioView) -> list[dict]:
        """Paper vs actual, per instrument: quantities, EUR values and the difference."""
        actual = {p.instrument.id: p for p in view.positions}
        paper = {p.instrument_id: p for p in self.positions(session)}
        rows = []
        for inst_id in sorted(
            set(actual) | set(paper),
            key=lambda i: -(actual[i].value_eur or 0) if i in actual else 0,
        ):
            inst = session.get(Instrument, inst_id)
            a, p = actual.get(inst_id), paper.get(inst_id)
            price = a.price if a and a.price is not None else (p.avg_cost if p else None)
            ccy = a.position.currency if a else (p.currency if p else inst.currency)
            rate = view.fx.get(ccy)
            per_eur = rate.per_eur if rate and rate.per_eur else 1.0
            a_qty = a.position.quantity if a else 0.0
            p_qty = p.quantity if p else 0.0
            a_eur = a.value_eur if a else 0.0
            p_eur = (p_qty * price / per_eur) if price is not None else None
            if inst.kind == InstrumentKind.cash:
                p_eur = p_qty / per_eur
            rows.append(
                {
                    "ticker": inst.ticker,
                    "kind": inst.kind.value,
                    "actual_qty": a_qty,
                    "paper_qty": p_qty,
                    "actual_eur": a_eur,
                    "paper_eur": p_eur,
                    "diff_eur": (p_eur or 0) - (a_eur or 0),
                    "currency": ccy,
                }
            )
        return rows

    def fills(self, session: Session, limit: int = 50) -> list[PaperFill]:
        return list(
            session.exec(select(PaperFill).order_by(PaperFill.id.desc()).limit(limit)).all()
        )
