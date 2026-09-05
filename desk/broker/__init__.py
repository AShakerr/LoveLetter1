"""Broker selection: DESK_BROKER=paper (default) | ibkr | alpaca."""

from __future__ import annotations

from sqlmodel import Session

from desk.broker.base import Broker, Costs, Fill, Order
from desk.broker.live import AlpacaBroker, IBKRBroker
from desk.broker.paper import PaperBroker
from desk.config import Settings, get_settings

__all__ = [
    "Broker",
    "Costs",
    "Fill",
    "Order",
    "PaperBroker",
    "IBKRBroker",
    "AlpacaBroker",
    "get_broker",
]


def get_broker(session: Session, settings: Settings | None = None):
    settings = settings or get_settings()
    name = (settings.broker or "paper").lower()
    if name == "paper":
        return PaperBroker(session, settings)
    if name == "ibkr":
        return IBKRBroker()
    if name == "alpaca":
        return AlpacaBroker()
    raise ValueError(f"unknown DESK_BROKER {name!r}")
