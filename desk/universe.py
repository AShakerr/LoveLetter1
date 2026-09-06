"""The instrument universe lives in config/universe.yaml and is upserted into `instruments`."""

from __future__ import annotations

from pathlib import Path

import yaml
from sqlmodel import Session, select

from desk.config import get_settings
from desk.models import Instrument, InstrumentKind


def load_universe(path: Path | None = None) -> list[dict]:
    path = path or (get_settings().config_dir / "universe.yaml")
    with open(path, encoding="utf-8") as fh:
        doc = yaml.safe_load(fh) or {}
    items = doc.get("instruments") or []
    for it in items:
        InstrumentKind(it["kind"])  # validate early
    return items


def sync_instruments(session: Session, items: list[dict] | None = None) -> int:
    """Upsert instruments by ticker. Returns number of rows created or updated."""
    items = items if items is not None else load_universe()
    changed = 0
    for it in items:
        row = session.exec(select(Instrument).where(Instrument.ticker == it["ticker"])).first()
        payload = dict(
            name=it["name"],
            kind=InstrumentKind(it["kind"]),
            currency=it["currency"],
            exchange=it.get("exchange"),
            tradable=bool(it.get("tradable", False)),
            theme=it.get("theme"),
            sector=it.get("sector"),
            region=it.get("region"),
            source_symbol=it.get("source_symbol"),
            price_currency=it.get("price_currency"),
            price_note=it.get("price_note"),
            isin=it.get("isin"),
            stale_after_days=it.get("stale_after_days"),
        )
        if row is None:
            cc = it.get("composition_confirmed")
            session.add(
                Instrument(
                    ticker=it["ticker"],
                    composition_confirmed=None if cc is None else bool(cc),
                    **payload,
                )
            )
            changed += 1
        else:
            dirty = False
            for k, v in payload.items():
                if getattr(row, k) != v:
                    setattr(row, k, v)
                    dirty = True
            # the composition flag is user state once set; only initialise it from the universe file
            cc = it.get("composition_confirmed")
            if cc is not None and row.composition_confirmed is None:
                row.composition_confirmed = bool(cc)
                dirty = True
            if dirty:
                session.add(row)
                changed += 1
    # instruments dropped from the universe are never recommended again (kept for history)
    # (screener members are not in universe.yaml; their tradable flag belongs to the constituent refresh)
    known = {it["ticker"] for it in items}
    for row in session.exec(select(Instrument)).all():
        if row.ticker not in known and row.tradable and row.screener_member is None:
            row.tradable = False
            session.add(row)
            changed += 1
    session.commit()
    return changed


def instruments_by_ticker(session: Session) -> dict[str, Instrument]:
    return {i.ticker: i for i in session.exec(select(Instrument)).all()}
