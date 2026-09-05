"""Execution service: decision -> Order -> broker.submit(); paper settlement; paper vs actual.

Rules from docs/BRIEF.md 8b that hold in every mode:
- Mandatory-rule exits (SELL/TRIM) are submitted when the decision is created. Discretionary BUY/ADD stay pending
  until the user approves them; then the same submit() path is used.
- Never more than one order per instrument per day; retries reuse client_ref.
- Kill switch (data/KILL) halts all execution, paper included.
- Live adapters additionally require DESK_LIVE=1 and the notional caps.
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal

from sqlmodel import Session, select

from desk.broker import get_broker
from desk.broker.base import Order
from desk.broker.guards import GuardError, check_live_order, kill_switch_active
from desk.broker.paper import PAPER, PaperBroker
from desk.config import Settings, get_settings
from desk.models import Decision, FillRow, Instrument, InstrumentKind, OrderRow, Position
from desk.portfolio import PortfolioView, build_portfolio

log = logging.getLogger(__name__)

ACTIONABLE = {"BUY": "BUY", "ADD": "BUY", "TRIM": "SELL", "SELL": "SELL"}


class ExecutionError(Exception):
    pass


def client_ref(decision: Decision) -> str:
    return f"desk-{decision.id}-{decision.date.isoformat()}"


def order_for_decision(
    session: Session,
    decision: Decision,
    view: PortfolioView | None = None,
    settings: Settings | None = None,
) -> tuple[Order, float, float | None]:
    """Build the Order for a decision. Returns (order, notional_eur, reference_price)."""
    settings = settings or get_settings()
    view = view or build_portfolio(session, settings)
    inst = session.get(Instrument, decision.instrument_id)
    side = ACTIONABLE.get(decision.action)
    if side is None:
        raise ExecutionError(f"{decision.action} is not an executable action")
    rate = view.fx.get(inst.currency)
    per_eur = rate.per_eur if rate and rate.per_eur else 1.0
    ref = (decision.rules_json or {}).get("reference_price")
    if ref is None:
        pv = next((p for p in view.positions if p.instrument.id == inst.id), None)
        ref = pv.price if pv else None
    notional_native = (decision.size_pct or 0.0) * view.total_eur * per_eur
    if side == "BUY":
        if notional_native <= 0:
            raise ExecutionError("BUY/ADD decision has no size")
        return (
            Order(
                decision_id=decision.id,
                instrument_id=inst.id,
                side="BUY",
                notional=Decimal(str(round(notional_native, 2))),
                client_ref=client_ref(decision),
            ),
            notional_native / per_eur,
            ref,
        )
    # SELL / TRIM: quantity from the book the broker holds
    book = PAPER if settings.broker == "paper" else "manual"
    pos = session.exec(
        select(Position).where(
            Position.instrument_id == inst.id, Position.broker == book, Position.closed_at.is_(None)
        )
    ).first()
    if pos is None:
        pos = session.exec(
            select(Position).where(
                Position.instrument_id == inst.id,
                Position.broker == "manual",
                Position.closed_at.is_(None),
                Position.confirmed_by_user.is_(True),
            )
        ).first()
    if pos is None or pos.quantity <= 0:
        raise ExecutionError(f"no open position in {inst.ticker} to sell")
    if decision.action == "SELL":
        qty = pos.quantity
    else:
        qty = (
            min(pos.quantity, notional_native / ref)
            if ref
            else pos.quantity * (decision.size_pct or 0)
        )
    notional_eur = qty * (ref or pos.last_price or 0) / per_eur
    return (
        Order(
            decision_id=decision.id,
            instrument_id=inst.id,
            side="SELL",
            quantity=Decimal(str(round(qty, 6))),
            client_ref=client_ref(decision),
        ),
        notional_eur,
        ref,
    )


def existing_order(session: Session, decision: Decision) -> OrderRow | None:
    return session.exec(select(OrderRow).where(OrderRow.decision_id == decision.id)).first()


def submit_decision(
    session: Session,
    decision: Decision,
    *,
    view: PortfolioView | None = None,
    settings: Settings | None = None,
    today: dt.date | None = None,
) -> OrderRow:
    """Route a decision through the configured broker. Idempotent per decision (client_ref)."""
    settings = settings or get_settings()
    today = today or decision.date
    prior = existing_order(session, decision)
    if prior is not None:
        return prior
    inst = session.get(Instrument, decision.instrument_id)
    broker = get_broker(session, settings)

    def _reject(reason: str) -> OrderRow:
        row = OrderRow(
            decision_id=decision.id,
            instrument_id=inst.id,
            broker=broker.name,
            client_ref=client_ref(decision),
            side=ACTIONABLE.get(decision.action, "BUY"),
            currency=inst.currency,
            status="rejected",
            order_date=today,
            error=reason,
            created_at=dt.datetime.utcnow(),
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return row

    if kill_switch_active(settings):
        return _reject("kill switch active (data/KILL)")
    same_day = session.exec(
        select(OrderRow).where(
            OrderRow.instrument_id == inst.id,
            OrderRow.order_date == today,
            OrderRow.status.in_(["submitted", "filled", "partial"]),
        )
    ).first()
    if same_day is not None:
        return _reject(f"one order per instrument per day: order {same_day.client_ref} exists")
    try:
        order, notional_eur, ref = order_for_decision(session, decision, view, settings)
    except (ExecutionError, ValueError) as exc:
        return _reject(str(exc))
    if broker.mode == "live":
        try:
            check_live_order(
                session,
                broker=broker.name,
                notional_eur=notional_eur,
                today=today,
                instrument_id=inst.id,
                settings=settings,
            )
        except GuardError as exc:
            return _reject(str(exc))
        try:
            broker_id = broker.submit(order)
        except NotImplementedError as exc:
            return _reject(str(exc))
        row = OrderRow(
            decision_id=decision.id,
            instrument_id=inst.id,
            broker=broker.name,
            broker_order_id=broker_id,
            client_ref=order.client_ref,
            side=order.side,
            quantity=float(order.quantity) if order.quantity is not None else None,
            notional=float(order.notional) if order.notional is not None else None,
            currency=inst.currency,
            status="submitted",
            submitted_at=dt.datetime.utcnow(),
            order_date=today,
            reference_price=ref,
            created_at=dt.datetime.utcnow(),
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return row
    # paper
    broker.submit(order)
    row = session.exec(select(OrderRow).where(OrderRow.client_ref == order.client_ref)).one()
    row.order_date, row.reference_price = today, ref
    session.add(row)
    session.commit()
    session.refresh(row)
    return row


def settle_paper(session: Session, settings: Settings | None = None) -> list[FillRow]:
    """Fill submitted paper orders whose next session has prices. Skipped entirely under the kill switch."""
    settings = settings or get_settings()
    if kill_switch_active(settings):
        log.warning("kill switch active: paper settlement skipped")
        return []
    if settings.broker != "paper":
        return []
    from desk.fundamentals import latest_field_map

    caps = latest_field_map(session, "marketCap")
    return PaperBroker(session, settings).settle(market_cap=caps)


def seed_paper_book(session: Session, settings: Settings | None = None) -> int:
    settings = settings or get_settings()
    return PaperBroker(session, settings).seed_from_actual()


def paper_vs_actual(
    session: Session, settings: Settings | None = None
) -> tuple[PortfolioView, PortfolioView, list[dict]]:
    """Both books valued the same way; rows sorted by actual value."""
    settings = settings or get_settings()
    actual = build_portfolio(session, settings, broker="manual")
    paper = build_portfolio(session, settings, broker=PAPER)
    a = {p.instrument.id: p for p in actual.positions}
    b = {p.instrument.id: p for p in paper.positions}
    rows = []
    for iid in sorted(
        set(a) | set(b), key=lambda i: -((a[i].value_eur or 0) if i in a else (b[i].value_eur or 0))
    ):
        inst = session.get(Instrument, iid)
        pa, pb = a.get(iid), b.get(iid)
        rows.append(
            {
                "ticker": inst.ticker,
                "kind": inst.kind.value,
                "actual_qty": pa.position.quantity if pa else 0.0,
                "paper_qty": pb.position.quantity if pb else 0.0,
                "actual_eur": (pa.value_eur or 0.0) if pa else 0.0,
                "paper_eur": (pb.value_eur or 0.0) if pb else 0.0,
                "diff_eur": ((pb.value_eur or 0.0) if pb else 0.0)
                - ((pa.value_eur or 0.0) if pa else 0.0),
                "currency": (pa or pb).position.currency,
                "cash": inst.kind == InstrumentKind.cash,
            }
        )
    return actual, paper, rows


def recent_orders(session: Session, limit: int = 30) -> list[tuple[OrderRow, FillRow | None]]:
    orders = session.exec(select(OrderRow).order_by(OrderRow.id.desc()).limit(limit)).all()
    out = []
    for o in orders:
        f = session.exec(
            select(FillRow).where(FillRow.order_id == o.id).order_by(FillRow.id.desc()).limit(1)
        ).first()
        out.append((o, f))
    return out
