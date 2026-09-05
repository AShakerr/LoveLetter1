"""Live adapters (docs/BRIEF.md 8b), phase 5 only. Both raise NotImplementedError on every call."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from desk.broker.base import Fill, Order
from desk.models import Instrument, Position


class _Stub:
    mode = "live"
    name = "stub"

    def _no(self, what: str):
        raise NotImplementedError(
            f"{self.name}: {what} is not implemented until phase 5 (docs/BRIEF.md 8b)"
        )

    def positions(self) -> list[Position]:
        self._no("positions()")

    def cash(self) -> dict[str, Decimal]:
        self._no("cash()")

    def submit(self, order: Order) -> str:
        self._no("submit()")

    def cancel(self, order_id: str) -> None:
        self._no("cancel()")

    def fills(self, since: dt.datetime) -> list[Fill]:
        self._no("fills()")

    def is_tradable(self, instrument: Instrument) -> bool:
        self._no("is_tradable()")


class IBKRBroker(_Stub):
    """Interactive Brokers via ib_async against an IB Gateway container. The one to build in phase 5."""

    name = "ibkr"


class AlpacaBroker(_Stub):
    """US stocks and crypto only; fallback / US-only sub-book. Stub."""

    name = "alpaca"
