"""Broker interface (docs/BRIEF.md 8b). Fixed from phase 3 so paper -> live changes one config line."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path
from typing import Literal, Protocol

import yaml
from pydantic import BaseModel, model_validator

from desk.models import Instrument, InstrumentKind, Position


class Order(BaseModel):
    decision_id: int
    instrument_id: int
    side: Literal["BUY", "SELL"]
    quantity: Decimal | None = None  # exactly one of quantity / notional
    notional: Decimal | None = None  # in instrument currency
    order_type: Literal["MARKET", "LIMIT"] = "MARKET"
    limit_price: Decimal | None = None
    time_in_force: Literal["DAY", "GTC"] = "DAY"
    client_ref: str  # f"desk-{decision_id}-{date}" so retries are idempotent

    @model_validator(mode="after")
    def _one_of(self) -> Order:
        if (self.quantity is None) == (self.notional is None):
            raise ValueError("exactly one of quantity / notional")
        if self.order_type == "LIMIT" and self.limit_price is None:
            raise ValueError("LIMIT orders need limit_price")
        return self


class Fill(BaseModel):
    order_id: str
    filled_at: dt.datetime
    quantity: Decimal
    price: Decimal
    fees: Decimal
    currency: str
    slippage_bps: Decimal | None = None  # vs reference price recorded on the decision


class Broker(Protocol):
    name: str
    mode: Literal["paper", "live"]

    def positions(self) -> list[Position]: ...

    def cash(self) -> dict[str, Decimal]: ...  # per currency

    def submit(self, order: Order) -> str: ...  # returns broker order id

    def cancel(self, order_id: str) -> None: ...

    def fills(self, since: dt.datetime) -> list[Fill]: ...

    def is_tradable(self, instrument: Instrument) -> bool: ...


class Costs:
    """config/costs.yaml: spread in bps by instrument class, flat fee by venue."""

    def __init__(self, doc: dict | None = None) -> None:
        doc = doc or {}
        self.spread_bps = {"etf": 5.0, "us_large_cap": 10.0, "default": 25.0, "crypto": 50.0}
        self.spread_bps.update({k: float(v) for k, v in (doc.get("spread_bps") or {}).items()})
        self.flat_fee = {"default": 0.0}
        self.flat_fee.update({k: float(v) for k, v in (doc.get("flat_fee") or {}).items()})
        self.us_large_cap_min_cap = float(doc.get("us_large_cap_min_market_cap", 1e10))

    @classmethod
    def load(cls, path: Path) -> Costs:
        if not path.exists():
            return cls()
        return cls(yaml.safe_load(path.read_text(encoding="utf-8")) or {})

    def instrument_class(self, inst: Instrument, market_cap: float | None = None) -> str:
        if inst.kind == InstrumentKind.etf:
            return "etf"
        if inst.kind == InstrumentKind.crypto:
            return "crypto"
        if inst.kind == InstrumentKind.stock and (inst.exchange or "").upper() in (
            "NYSE",
            "NASDAQ",
        ):
            if market_cap is None or market_cap >= self.us_large_cap_min_cap:
                return "us_large_cap"
        return "default"

    def spread_for(self, inst: Instrument, market_cap: float | None = None) -> float:
        return self.spread_bps.get(
            self.instrument_class(inst, market_cap), self.spread_bps["default"]
        )

    def fee_for(self, inst: Instrument) -> float:
        return self.flat_fee.get(inst.exchange or "", self.flat_fee.get("default", 0.0))
