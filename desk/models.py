"""SQLModel tables. This is the full schema from docs/BRIEF.md section 4 so later phases do not migrate.

Phase 1 populates: instruments, prices, observations, news_sentiment, fetch_runs.
"""

from __future__ import annotations

import datetime as dt
from enum import StrEnum

from sqlalchemy import JSON, Column, UniqueConstraint
from sqlmodel import Field, SQLModel


class InstrumentKind(StrEnum):
    stock = "stock"
    etf = "etf"
    commodity = "commodity"
    crypto = "crypto"
    private = "private"
    # not in the brief's list; needed so benchmark indices and FX pairs can live in `prices`
    # with a tradable=False flag rather than being smuggled into `observations`.
    index = "index"
    fx = "fx"
    cash = "cash"
    other = (
        "other"  # unidentified lines (e.g. behind Revolut's "Show more") until the user names them
    )


class Instrument(SQLModel, table=True):
    __tablename__ = "instruments"
    id: int | None = Field(default=None, primary_key=True)
    ticker: str = Field(index=True, unique=True)
    name: str
    kind: InstrumentKind
    currency: str
    exchange: str | None = None
    tradable: bool = False
    theme: str | None = None
    sector: str | None = None
    region: str | None = None
    # symbol used at the data source when it differs from the display ticker (e.g. VUSA -> VUSA.AS)
    source_symbol: str | None = None
    isin: str | None = None
    # None for ordinary instruments. False for pots whose composition the user has not confirmed yet:
    # while False, max_position / max_theme raise a REVIEW flag instead of a MANDATORY TRIM.
    composition_confirmed: bool | None = None
    # override for the stale_data rule and the red as-of date (e.g. 90 for an SPV marked to a stated value)
    stale_after_days: int | None = None
    # screener universe membership: sp500 | stoxx600 | safra_focus_list (docs/BRIEF.md 8c); None for core names
    screener_member: str | None = Field(default=None, index=True)
    screener_dropped: bool = False  # left the constituent list at the last refresh; kept, flagged


class Price(SQLModel, table=True):
    __tablename__ = "prices"
    __table_args__ = (UniqueConstraint("instrument_id", "date", name="uq_price_instrument_date"),)
    id: int | None = Field(default=None, primary_key=True)
    instrument_id: int = Field(foreign_key="instruments.id", index=True)
    date: dt.date = Field(index=True)
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float
    volume: float | None = None
    source: str
    fetched_at: dt.datetime


class Observation(SQLModel, table=True):
    """A single dated number from a named series, e.g. series='DGS10'."""

    __tablename__ = "observations"
    __table_args__ = (
        UniqueConstraint("series", "date", "source", name="uq_obs_series_date_source"),
    )
    id: int | None = Field(default=None, primary_key=True)
    series: str = Field(index=True)
    date: dt.date = Field(index=True)
    value: float
    source: str
    fetched_at: dt.datetime
    meta: dict | None = Field(default=None, sa_column=Column(JSON))


class NewsSentiment(SQLModel, table=True):
    __tablename__ = "news_sentiment"
    __table_args__ = (
        UniqueConstraint("instrument_id", "topic", "date", "source", name="uq_news_key"),
    )
    id: int | None = Field(default=None, primary_key=True)
    instrument_id: int | None = Field(default=None, foreign_key="instruments.id", index=True)
    topic: str | None = Field(default=None, index=True)
    date: dt.date = Field(index=True)
    score: float
    volume: float | None = None
    source: str
    fetched_at: dt.datetime


class FetchRun(SQLModel, table=True):
    """One row per fetcher per run: what happened, how many rows, and whether we fell back to cache."""

    __tablename__ = "fetch_runs"
    id: int | None = Field(default=None, primary_key=True)
    source: str = Field(index=True)
    started_at: dt.datetime
    finished_at: dt.datetime | None = None
    status: str = "running"  # running | ok | cached | failed | skipped
    rows: int = 0
    error: str | None = None


class Pot(StrEnum):
    brokerage = "brokerage"
    commodities = "commodities"
    robo = "robo"
    external = (
        "external"  # held outside Revolut (SPVs, other brokers); valued from the user's statement
    )


class Position(SQLModel, table=True):
    __tablename__ = "positions"
    id: int | None = Field(default=None, primary_key=True)
    instrument_id: int = Field(foreign_key="instruments.id", index=True)
    quantity: float
    avg_cost: float
    currency: str
    pot: Pot = Pot.brokerage
    as_of: dt.date
    confirmed_by_user: bool = False
    # what the screenshot / seed said, used when no market price exists for the instrument
    last_price: float | None = None
    value_native: float | None = None
    return_pct: float | None = None
    source: str = "manual"  # manual | seed | screenshot | decision | paper
    # which book the row belongs to: "manual" (what the user actually holds, confirmed) or "paper"
    # (the shadow book the PaperBroker maintains through fills). Same table so the UI is identical.
    broker: str = Field(default="manual", index=True)
    batch: str | None = Field(default=None, index=True)
    note: str | None = None
    # phase 3: per-position overrides and thesis
    stop_pct: float | None = None
    kill_condition: str | None = None  # the thesis, free text
    kill_predicate: str | None = None  # first mandatory predicate (legacy single-predicate form)
    # full form from docs/seed/kill_conditions_*.yaml:
    # {"thesis": str, "kills": [{"predicate"|"human": str, "severity": "mandatory"|"review", "note": str}],
    #  "add_blocked_while": str|None, "pre_condition": str|None, "theme": str|None}
    kill_json: dict | None = Field(default=None, sa_column=Column(JSON))
    closed_at: dt.datetime | None = None


class Report(SQLModel, table=True):
    __tablename__ = "reports"
    id: int | None = Field(default=None, primary_key=True)
    publisher: str
    kind: str
    date: dt.date
    filename: str
    sha256: str = Field(index=True, unique=True)
    extracted_at: dt.datetime | None = None
    raw_json: dict | None = Field(default=None, sa_column=Column(JSON))
    flagged: bool = False  # extraction failed validation twice; raw_json is null
    flag_reason: str | None = None


class HouseView(SQLModel, table=True):
    __tablename__ = "house_views"
    id: int | None = Field(default=None, primary_key=True)
    report_id: int = Field(foreign_key="reports.id", index=True)
    scope: str = Field(index=True)
    key: str = Field(index=True)
    stance: str | None = None
    value: str | None = None
    changed_from: str | None = None
    quote: str | None = None
    page: int | None = None


class Score(SQLModel, table=True):
    __tablename__ = "scores"
    id: int | None = Field(default=None, primary_key=True)
    instrument_id: int = Field(foreign_key="instruments.id", index=True)
    date: dt.date = Field(index=True)
    total: float
    f_safra: float
    f_regime: float
    f_portfolio: float
    f_valuation: float
    f_momentum: float
    f_season: float
    f_crowd: float = 0.0  # docs/BRIEF.md 7b
    inputs_json: dict | None = Field(default=None, sa_column=Column(JSON))


class RuleFired(SQLModel, table=True):
    __tablename__ = "rules_fired"
    id: int | None = Field(default=None, primary_key=True)
    position_id: int | None = Field(default=None, foreign_key="positions.id", index=True)
    instrument_id: int | None = Field(default=None, foreign_key="instruments.id", index=True)
    date: dt.date = Field(index=True)
    rule: str
    severity: str  # mandatory | review
    detail_json: dict | None = Field(default=None, sa_column=Column(JSON))


class Decision(SQLModel, table=True):
    """Append-only. Content is never updated; only user_status / user_note / executed_at change."""

    __tablename__ = "decisions"
    id: int | None = Field(default=None, primary_key=True)
    date: dt.date = Field(index=True)
    instrument_id: int = Field(foreign_key="instruments.id", index=True)
    action: str  # BUY | ADD | HOLD | TRIM | SELL | AVOID
    size_pct: float | None = None
    score_id: int | None = Field(default=None, foreign_key="scores.id")
    rules_json: dict | None = Field(default=None, sa_column=Column(JSON))
    reasoning_md: str
    created_at: dt.datetime
    user_status: str = "pending"  # pending | approved | executed | skipped | overridden | deferred
    user_note: str | None = None
    executed_at: dt.datetime | None = None


class Regime(SQLModel, table=True):
    __tablename__ = "regime"
    id: int | None = Field(default=None, primary_key=True)
    date: dt.date = Field(index=True, unique=True)
    label: str
    inflation_state: str
    policy_state: str
    oil_state: str
    vol_state: str
    inputs_json: dict | None = Field(default=None, sa_column=Column(JSON))


class OrderRow(SQLModel, table=True):
    """docs/BRIEF.md 8b `orders`. Positions change only through fills, never directly."""

    __tablename__ = "orders"
    id: int | None = Field(default=None, primary_key=True)
    decision_id: int | None = Field(default=None, foreign_key="decisions.id", index=True)
    instrument_id: int = Field(foreign_key="instruments.id", index=True)
    broker: str = Field(index=True)  # paper | ibkr | alpaca | manual
    broker_order_id: str | None = None
    client_ref: str = Field(
        index=True, unique=True
    )  # desk-<decision_id>-<date>: retries are idempotent
    side: str  # BUY | SELL
    quantity: float | None = None
    notional: float | None = None  # in instrument currency
    currency: str
    order_type: str = "MARKET"
    limit_price: float | None = None
    time_in_force: str = "DAY"
    status: str = "pending"  # pending | submitted | filled | partial | cancelled | rejected
    submitted_at: dt.datetime | None = None
    order_date: dt.date = Field(
        index=True
    )  # the session the order belongs to; paper fills at the next open
    reference_price: float | None = None  # close recorded on the decision, for slippage
    error: str | None = None
    created_at: dt.datetime


class FillRow(SQLModel, table=True):
    __tablename__ = "fills"
    id: int | None = Field(default=None, primary_key=True)
    order_id: int = Field(foreign_key="orders.id", index=True)
    filled_at: dt.datetime
    quantity: float
    price: float
    fees: float = 0.0
    currency: str
    slippage_bps: float | None = None
    note: str | None = None


class Fundamental(SQLModel, table=True):
    """docs/BRIEF.md 7c: one row per (instrument, date, field, source), refreshed weekly."""

    __tablename__ = "fundamentals"
    __table_args__ = (
        UniqueConstraint("instrument_id", "date", "field", "source", name="uq_fund_key"),
    )
    id: int | None = Field(default=None, primary_key=True)
    instrument_id: int = Field(foreign_key="instruments.id", index=True)
    date: dt.date = Field(index=True)
    field: str = Field(index=True)
    value: float | None = None
    source: str


class Event(SQLModel, table=True):
    """Scheduled events with consensus and actual (docs/BRIEF.md 7b): macro, central bank, earnings."""

    __tablename__ = "events"
    __table_args__ = (UniqueConstraint("date", "name", "instrument_id", name="uq_event_key"),)
    id: int | None = Field(default=None, primary_key=True)
    date: dt.date = Field(index=True)
    name: str
    kind: str  # macro | central_bank | earnings
    instrument_id: int | None = Field(default=None, foreign_key="instruments.id", index=True)
    consensus: float | None = None
    actual: float | None = None
    market_implied: float | None = None
    higher_is_good: bool = True
    favours: list | None = Field(default=None, sa_column=Column(JSON))
    hurts: list | None = Field(default=None, sa_column=Column(JSON))
    source: str = "config"
    updated_at: dt.datetime | None = None

    @property
    def surprise(self) -> float | None:
        if self.actual is None or self.consensus is None:
            return None
        return self.actual - self.consensus


class ScreenerRow(SQLModel, table=True):
    """docs/BRIEF.md 8c: the day's top and bottom names with their breakdown and gates."""

    __tablename__ = "screener"
    __table_args__ = (UniqueConstraint("date", "instrument_id", name="uq_screener_key"),)
    id: int | None = Field(default=None, primary_key=True)
    date: dt.date = Field(index=True)
    instrument_id: int = Field(foreign_key="instruments.id", index=True)
    rank: int
    total: float
    factors_json: dict | None = Field(default=None, sa_column=Column(JSON))
    gates_json: dict | None = Field(default=None, sa_column=Column(JSON))
