"""Kill conditions: a thesis (free text) plus a machine-checkable predicate in the DSL of desk/predicates.py.

Seeded from docs/seed/kill_conditions_<date>.yaml. The file is the user's; the loader accepts either of
these shapes and ignores unknown keys:

    positions:                       # attached to open positions with that ticker
      TSLA:
        thesis: "..."                # or kill_condition:
        predicate: "close() < avg_cost() * 0.82"
    candidates:                      # attached to BUY decisions for these tickers
      NVDA: {thesis: "...", predicate: "..."}

or a flat mapping {TICKER: {thesis, predicate}} which is treated as both.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml
from sqlmodel import Session, select

from desk.config import Settings, get_settings
from desk.models import Instrument, Position

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class KillCondition:
    ticker: str
    thesis: str | None
    predicate: str | None


def _parse_entry(ticker: str, entry) -> KillCondition:
    if isinstance(entry, str):
        return KillCondition(ticker, None, entry)
    entry = entry or {}
    thesis = entry.get("thesis") or entry.get("kill_condition") or entry.get("text")
    predicate = entry.get("predicate") or entry.get("rule") or entry.get("expr")
    return KillCondition(ticker.upper(), thesis, predicate)


def load_file(path: Path) -> tuple[dict[str, KillCondition], dict[str, KillCondition]]:
    """Returns (for positions, for buy candidates)."""
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    positions: dict[str, KillCondition] = {}
    candidates: dict[str, KillCondition] = {}
    if "positions" in doc or "candidates" in doc:
        for t, e in (doc.get("positions") or {}).items():
            positions[t.upper()] = _parse_entry(t, e)
        for t, e in (doc.get("candidates") or {}).items():
            candidates[t.upper()] = _parse_entry(t, e)
    else:
        for t, e in doc.items():
            if isinstance(e, (dict, str)):
                kc = _parse_entry(t, e)
                positions[kc.ticker] = kc
                candidates[kc.ticker] = kc
    return positions, candidates


def latest_seed_file(settings: Settings) -> Path | None:
    files = sorted(settings.seed_dir.glob("kill_conditions_*.yaml")) + sorted(
        settings.seed_dir.glob("kill_conditions_*.yml")
    )
    return files[-1] if files else None


def candidate_conditions(settings: Settings | None = None) -> dict[str, KillCondition]:
    settings = settings or get_settings()
    path = latest_seed_file(settings)
    if path is None:
        return {}
    return load_file(path)[1]


def apply_to_positions(
    session: Session, conditions: dict[str, KillCondition], overwrite: bool = False
) -> int:
    """Attach conditions to open positions with matching tickers. Returns rows updated."""
    n = 0
    for ticker, kc in conditions.items():
        inst = session.exec(select(Instrument).where(Instrument.ticker == ticker)).first()
        if inst is None:
            log.warning("kill condition for unknown ticker %s ignored", ticker)
            continue
        rows = session.exec(
            select(Position).where(Position.instrument_id == inst.id, Position.closed_at.is_(None))
        ).all()
        for row in rows:
            if row.kill_predicate and not overwrite:
                continue
            row.kill_condition, row.kill_predicate = kc.thesis, kc.predicate
            session.add(row)
            n += 1
    session.commit()
    return n


def load_seed_kill_conditions(session: Session, settings: Settings | None = None) -> dict:
    settings = settings or get_settings()
    path = latest_seed_file(settings)
    if path is None:
        return {"status": "missing", "note": "no docs/seed/kill_conditions_*.yaml"}
    positions, candidates = load_file(path)
    n = apply_to_positions(session, positions)
    return {
        "status": "ok",
        "file": path.name,
        "positions": len(positions),
        "candidates": len(candidates),
        "rows_updated": n,
    }
