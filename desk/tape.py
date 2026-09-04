"""Read model for the dashboard tape. Every number comes with its source, date and fetch time."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from sqlmodel import Session, select

from desk.models import FetchRun, Instrument, Observation, Price
from desk.sources.manual import STALE_AFTER_DAYS as MANUAL_STALE_DAYS


@dataclass(frozen=True)
class TapeSpec:
    label: str
    kind: Literal["price", "observation"]
    key: str  # ticker for price, series for observation
    unit: str = ""
    decimals: int = 2
    change: Literal["pct", "abs", "none"] = "pct"
    frequency: Literal["daily", "monthly"] = "daily"


# Layout, not data. Values are looked up in the database.
TAPE: list[TapeSpec] = [
    TapeSpec("S&P 500", "price", "^GSPC", decimals=0),
    TapeSpec("Brent", "price", "BZ=F", unit="$"),
    TapeSpec("Gold", "price", "GC=F", unit="$", decimals=0),
    TapeSpec("US 10y", "observation", "DGS10", unit="%", change="abs"),
    TapeSpec("VIX", "price", "^VIX", change="abs"),
    TapeSpec("Fed funds", "observation", "DFF", unit="%", change="abs"),
    TapeSpec(
        "EZ HICP y/y",
        "observation",
        "EZ_HICP",
        unit="%",
        decimals=1,
        change="abs",
        frequency="monthly",
    ),
    TapeSpec("Bitcoin", "price", "BTC-USD", unit="$", decimals=0),
]

MORE: list[TapeSpec] = [
    TapeSpec("Nasdaq 100", "price", "^NDX", decimals=0),
    TapeSpec("DAX", "price", "^GDAXI", decimals=0),
    TapeSpec("Euro Stoxx 50", "price", "^STOXX50E", decimals=0),
    TapeSpec("WTI", "price", "CL=F", unit="$"),
    TapeSpec("Copper", "price", "HG=F", unit="$", decimals=3),
    TapeSpec("Silver", "price", "SI=F", unit="$"),
    TapeSpec("EUR/USD", "price", "EURUSD=X", decimals=4),
    TapeSpec("EUR/GBP", "price", "EURGBP=X", decimals=4),
    TapeSpec("DXY", "price", "DXY", decimals=2),
    TapeSpec("Ethereum", "price", "ETH-USD", unit="$", decimals=0),
    TapeSpec("US 2y", "observation", "DGS2", unit="%", change="abs"),
    TapeSpec("US 30y", "observation", "DGS30", unit="%", change="abs"),
    TapeSpec("10y–2y", "observation", "T10Y2Y", unit="pp", change="abs"),
    TapeSpec(
        "US unemployment",
        "observation",
        "UNRATE",
        unit="%",
        decimals=1,
        change="abs",
        frequency="monthly",
    ),
    TapeSpec("ECB deposit rate", "observation", "ECB_DEPO", unit="%", change="abs"),
    TapeSpec(
        "EZ core HICP y/y",
        "observation",
        "EZ_HICP_CORE",
        unit="%",
        decimals=1,
        change="abs",
        frequency="monthly",
    ),
    TapeSpec("Fear & Greed", "observation", "CNN_FEAR_GREED", decimals=0, change="abs"),
    TapeSpec("EGX30", "observation", "EGX30", decimals=0),
    TapeSpec(
        "CBE deposit rate", "observation", "CBE_DEPOSIT_RATE", unit="%", decimals=2, change="abs"
    ),
]


@dataclass
class TapeItem:
    spec: TapeSpec
    value: float | None = None
    prev: float | None = None
    as_of: date | None = None
    prev_date: date | None = None
    source: str | None = None
    fetched_at: datetime | None = None
    note: str | None = None

    @property
    def change(self) -> float | None:
        if self.value is None or self.prev is None:
            return None
        if self.spec.change == "pct":
            return (self.value / self.prev - 1) * 100 if self.prev else None
        if self.spec.change == "abs":
            return self.value - self.prev
        return None

    @property
    def age_days(self) -> int | None:
        return None if self.as_of is None else (date.today() - self.as_of).days

    @property
    def freshness(self) -> str:
        """fresh | aging | stale | missing.

        Daily series: fresh <= 3 days, aging <= 7, stale beyond (brief: stale_data rule is 7 days).
        Monthly series are dated by period start, so allow one release cycle: fresh <= 45, aging <= 75.
        Manual data has its own 14-day red line.
        """
        age = self.age_days
        if age is None:
            return "missing"
        if self.source and self.source.endswith("manual"):
            return "stale" if age > MANUAL_STALE_DAYS else "fresh"
        fresh, aging = (45, 75) if self.spec.frequency == "monthly" else (3, 7)
        if age <= fresh:
            return "fresh"
        if age <= aging:
            return "aging"
        return "stale"

    @property
    def is_manual(self) -> bool:
        return bool(self.source and self.source.endswith("manual"))


def _latest_two_prices(session: Session, ticker: str) -> list[Price]:
    inst = session.exec(select(Instrument).where(Instrument.ticker == ticker)).first()
    if inst is None:
        return []
    return list(
        session.exec(
            select(Price).where(Price.instrument_id == inst.id).order_by(Price.date.desc()).limit(2)
        ).all()
    )


def _latest_two_obs(session: Session, series: str) -> list[Observation]:
    return list(
        session.exec(
            select(Observation)
            .where(Observation.series == series)
            .order_by(Observation.date.desc(), Observation.fetched_at.desc())
            .limit(2)
        ).all()
    )


def load_tape(session: Session, specs: list[TapeSpec] | None = None) -> list[TapeItem]:
    items: list[TapeItem] = []
    for spec in specs or TAPE:
        item = TapeItem(spec=spec)
        rows = (
            _latest_two_prices(session, spec.key)
            if spec.kind == "price"
            else _latest_two_obs(session, spec.key)
        )
        if rows:
            r0 = rows[0]
            item.value = r0.close if spec.kind == "price" else r0.value
            item.as_of, item.source, item.fetched_at = r0.date, r0.source, r0.fetched_at
            if spec.kind == "observation" and r0.meta:
                item.note = r0.meta.get("note") or r0.meta.get("rating")
            if len(rows) > 1:
                r1 = rows[1]
                item.prev = r1.close if spec.kind == "price" else r1.value
                item.prev_date = r1.date
        items.append(item)
    return items


def latest_runs(session: Session) -> list[FetchRun]:
    """Most recent fetch_runs row per source."""
    rows = session.exec(select(FetchRun).order_by(FetchRun.started_at.desc())).all()
    seen: dict[str, FetchRun] = {}
    for r in rows:
        seen.setdefault(r.source, r)
    return sorted(seen.values(), key=lambda r: r.source)
