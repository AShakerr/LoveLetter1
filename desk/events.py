"""Event calendar (docs/BRIEF.md 7b): config/events.yaml (macro, central bank) plus earnings dates from the weekly
fundamentals job. Consensus before, actual after; the surprise sign feeds the crowd factor and the deferral rule."""

from __future__ import annotations

import datetime as dt
from typing import Any

import yaml
from sqlmodel import Session, select

from desk.config import Settings, get_settings
from desk.models import Event, Instrument
from desk.sources.base import utcnow

MACRO_KINDS = ("macro", "central_bank")


def _date(v: Any) -> dt.date:
    return v if isinstance(v, dt.date) else dt.date.fromisoformat(str(v))


def load_events_config(session: Session, settings: Settings | None = None) -> int:
    """Upsert config/events.yaml into `events`. Returns rows written or updated."""
    settings = settings or get_settings()
    path = settings.config_dir / "events.yaml"
    if not path.exists():
        return 0
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    n = 0
    for e in doc.get("events") or []:
        inst_id = None
        if e.get("ticker"):
            inst = session.exec(
                select(Instrument).where(Instrument.ticker == str(e["ticker"]).upper())
            ).first()
            inst_id = inst.id if inst else None
        d = _date(e["date"])
        row = session.exec(
            select(Event).where(
                Event.date == d, Event.name == e["name"], Event.instrument_id == inst_id
            )
        ).first()
        payload = dict(
            kind=e.get("kind", "macro"),
            consensus=e.get("consensus"),
            actual=e.get("actual"),
            market_implied=e.get("market_implied"),
            higher_is_good=bool(e.get("higher_is_good", True)),
            favours=list(e.get("favours") or []),
            hurts=list(e.get("hurts") or []),
            source="config",
            updated_at=utcnow(),
        )
        if row is None:
            session.add(Event(date=d, name=e["name"], instrument_id=inst_id, **payload))
            n += 1
        else:
            changed = False
            for k, v in payload.items():
                if k != "updated_at" and getattr(row, k) != v:
                    setattr(row, k, v)
                    changed = True
            if changed:
                row.updated_at = utcnow()
                session.add(row)
                n += 1
    session.commit()
    return n


def add_earnings_event(
    session: Session,
    instrument: Instrument,
    on: dt.date,
    consensus_eps: float | None,
    actual_eps: float | None,
    source: str = "yfinance",
) -> Event:
    row = session.exec(
        select(Event).where(
            Event.date == on, Event.instrument_id == instrument.id, Event.kind == "earnings"
        )
    ).first()
    if row is None:
        row = Event(
            date=on,
            name=f"{instrument.ticker} earnings",
            kind="earnings",
            instrument_id=instrument.id,
            higher_is_good=True,
            favours=[],
            hurts=[],
            source=source,
        )
    row.consensus, row.actual, row.updated_at = consensus_eps, actual_eps, utcnow()
    session.add(row)
    session.commit()
    return row


def trading_days_ahead(today: dt.date, n: int) -> dt.date:
    d = today
    left = n
    while left > 0:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            left -= 1
    return d


def upcoming(
    session: Session, today: dt.date, trading_days: int = 2, instrument_id: int | None = None
) -> list[Event]:
    """Macro / central-bank events in the window, plus the instrument's own earnings."""
    until = trading_days_ahead(today, trading_days)
    rows = session.exec(
        select(Event).where(Event.date >= today, Event.date <= until).order_by(Event.date)
    ).all()
    return [
        e
        for e in rows
        if e.kind in MACRO_KINDS or (instrument_id is not None and e.instrument_id == instrument_id)
    ]


def last_with_actual(
    session: Session,
    today: dt.date,
    *,
    instrument_id: int | None = None,
    theme: str | None = None,
    lookback_days: int = 45,
) -> Event | None:
    """Most recent completed event relevant to the instrument: its own earnings, else a macro event naming its theme."""
    since = today - dt.timedelta(days=lookback_days)
    rows = session.exec(
        select(Event)
        .where(Event.date <= today, Event.date >= since, Event.actual.is_not(None))
        .order_by(Event.date.desc())
    ).all()
    if instrument_id is not None:
        for e in rows:
            if e.kind == "earnings" and e.instrument_id == instrument_id:
                return e
    if theme:
        for e in rows:
            if e.kind in MACRO_KINDS and (theme in (e.favours or []) or theme in (e.hurts or [])):
                return e
    return None


def surprise_direction(event: Event, theme: str | None, instrument_id: int | None = None) -> int:
    """+1 when the outcome beat expectations in the instrument's favour, -1 against, 0 unknown."""
    s = event.surprise
    if s is None or s == 0:
        return 0
    beat = (s > 0) == bool(
        event.higher_is_good
    )  # outcome better than consensus in the event's own terms
    if event.kind == "earnings":
        return 1 if beat else -1
    if theme in (event.favours or []):
        return 1 if beat else -1
    if theme in (event.hurts or []):
        return -1 if beat else 1
    return 0
