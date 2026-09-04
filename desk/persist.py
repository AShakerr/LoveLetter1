"""Route observations into their tables. Idempotent: (series, date, source) and (instrument, date) are upserted."""

from __future__ import annotations

from collections import Counter

from sqlmodel import Session, select

from desk.models import FetchRun, Instrument, NewsSentiment, Price
from desk.models import Observation as ObservationRow
from desk.sources.base import FetchOutcome, Observation


def _instrument_map(session: Session) -> dict[str, Instrument]:
    return {i.ticker: i for i in session.exec(select(Instrument)).all()}


def persist_observations(session: Session, obs: list[Observation]) -> Counter:
    counts: Counter = Counter()
    instruments = _instrument_map(session)
    for o in obs:
        if o.is_price:
            inst = instruments.get(o.ticker or "")
            if inst is None:
                counts["skipped_unknown_ticker"] += 1
                continue
            row = session.exec(
                select(Price).where(Price.instrument_id == inst.id, Price.date == o.date)
            ).first()
            m = o.meta or {}
            if row is None:
                session.add(
                    Price(
                        instrument_id=inst.id,
                        date=o.date,
                        open=m.get("open"),
                        high=m.get("high"),
                        low=m.get("low"),
                        close=o.value,
                        volume=m.get("volume"),
                        source=o.source,
                        fetched_at=o.fetched_at,
                    )
                )
                counts["prices"] += 1
            elif row.close != o.value or row.source != o.source:
                row.open, row.high, row.low = m.get("open"), m.get("high"), m.get("low")
                row.close, row.volume, row.source, row.fetched_at = (
                    o.value,
                    m.get("volume"),
                    o.source,
                    o.fetched_at,
                )
                session.add(row)
                counts["prices_updated"] += 1
        elif o.is_news:
            inst_id = None
            if o.ticker is not None:
                inst = instruments.get(o.ticker)
                if inst is None:
                    counts["skipped_unknown_ticker"] += 1
                    continue
                inst_id = inst.id
            row = session.exec(
                select(NewsSentiment).where(
                    NewsSentiment.instrument_id == inst_id,
                    NewsSentiment.topic == o.topic,
                    NewsSentiment.date == o.date,
                    NewsSentiment.source == o.source,
                )
            ).first()
            vol = (o.meta or {}).get("volume")
            if row is None:
                session.add(
                    NewsSentiment(
                        instrument_id=inst_id,
                        topic=o.topic,
                        date=o.date,
                        score=o.value,
                        volume=vol,
                        source=o.source,
                        fetched_at=o.fetched_at,
                    )
                )
                counts["news"] += 1
            elif row.score != o.value or row.volume != vol:
                row.score, row.volume, row.fetched_at = o.value, vol, o.fetched_at
                session.add(row)
                counts["news_updated"] += 1
        else:
            row = session.exec(
                select(ObservationRow).where(
                    ObservationRow.series == o.series,
                    ObservationRow.date == o.date,
                    ObservationRow.source == o.source,
                )
            ).first()
            if row is None:
                session.add(
                    ObservationRow(
                        series=o.series,
                        date=o.date,
                        value=o.value,
                        source=o.source,
                        fetched_at=o.fetched_at,
                        meta=o.meta,
                    )
                )
                counts["observations"] += 1
            elif row.value != o.value:
                row.value, row.fetched_at, row.meta = o.value, o.fetched_at, o.meta
                session.add(row)
                counts["observations_updated"] += 1
    session.commit()
    return counts


def record_run(session: Session, outcome: FetchOutcome, rows: int) -> FetchRun:
    run = FetchRun(
        source=outcome.source,
        started_at=outcome.started_at,
        finished_at=outcome.finished_at,
        status=outcome.status,
        rows=rows,
        error=outcome.error,
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    return run
