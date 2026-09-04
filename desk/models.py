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
    source: str = "manual"  # manual | seed | screenshot
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
    user_status: str = "pending"  # pending | executed | skipped | overridden
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


class PaperPosition(SQLModel, table=True):
    """The paper book: what the portfolio would look like if every decision had been executed."""

    __tablename__ = "paper_positions"
    id: int | None = Field(default=None, primary_key=True)
    instrument_id: int = Field(foreign_key="instruments.id", index=True, unique=True)
    quantity: float
    avg_cost: float
    currency: str
    updated_at: dt.datetime


class PaperFill(SQLModel, table=True):
    __tablename__ = "paper_fills"
    id: int | None = Field(default=None, primary_key=True)
    decision_id: int | None = Field(default=None, foreign_key="decisions.id", index=True)
    instrument_id: int = Field(foreign_key="instruments.id", index=True)
    date: dt.date
    side: str  # buy | sell
    quantity: float
    price: float
    currency: str
    note: str | None = None
    created_at: dt.datetime
