"""Kill conditions: a thesis (free text) plus machine-checkable predicates in the DSL of desk/predicates.py.

Source of truth: docs/seed/kill_conditions_<date>.yaml. Shape:

    positions:
      TSLA:
        theme: ev_auto
        thesis: "..."
        kill:
          - {predicate: "close('TSLA') < 0.82 * avg_cost('TSLA')", severity: mandatory, note: "..."}
          - {predicate: "...", severity: review}
          - {human: "Listing completes ...", severity: review}     # not machine-checkable: always a REVIEW flag
        pre_condition: "..."          # documentation, shown with the position
        add_blocked_while: "theme_weight('gold') + theme_weight('materials_copper') > 35"
    candidates:
      EU_BROAD_ETF: {theme: eu_broad, thesis: "...", kill: [...]}    # attaches to BUYs by theme (or ticker)

Each position stores the parsed block in `positions.kill_json`; `kill_condition` holds the thesis and
`kill_predicate` the first mandatory predicate, for the single-predicate views.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from sqlmodel import Session, select

from desk.config import Settings, get_settings
from desk.models import Instrument, Position

log = logging.getLogger(__name__)

SEVERITIES = {"mandatory", "review"}


def normalise(ticker: str, entry: Any) -> dict[str, Any]:
    """Return the canonical kill block for one position/candidate."""
    if isinstance(entry, str):
        return {
            "ticker": ticker.upper(),
            "thesis": None,
            "kills": [{"predicate": entry, "severity": "mandatory", "note": None}],
            "add_blocked_while": None,
            "pre_condition": None,
            "theme": None,
        }
    entry = entry or {}
    kills: list[dict[str, Any]] = []
    raw_kills = entry.get("kill")
    if raw_kills is None and entry.get("predicate"):  # legacy single-predicate form
        raw_kills = [
            {
                "predicate": entry["predicate"],
                "severity": entry.get("severity", "mandatory"),
                "note": entry.get("note"),
            }
        ]
    for k in raw_kills or []:
        if isinstance(k, str):
            k = {"predicate": k}
        sev = str(k.get("severity") or "mandatory").lower()
        if sev not in SEVERITIES:
            sev = "review"
        item = {"severity": sev, "note": k.get("note")}
        if k.get("predicate"):
            item["predicate"] = str(k["predicate"]).strip()
        elif k.get("human"):
            item["human"] = str(k["human"]).strip()
        else:
            continue
        kills.append(item)
    thesis = entry.get("thesis") or entry.get("kill_condition") or entry.get("text")
    return {
        "ticker": ticker.upper(),
        "thesis": thesis.strip() if isinstance(thesis, str) else thesis,
        "kills": kills,
        "add_blocked_while": entry.get("add_blocked_while"),
        "pre_condition": entry.get("pre_condition"),
        "theme": entry.get("theme"),
    }


def first_mandatory_predicate(block: dict[str, Any] | None) -> str | None:
    for k in (block or {}).get("kills") or []:
        if k.get("predicate") and k.get("severity") == "mandatory":
            return k["predicate"]
    return None


def load_file(path: Path) -> tuple[dict[str, dict], dict[str, dict]]:
    """Returns (positions by ticker, candidates by placeholder name)."""
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    positions = {t.upper(): normalise(t, e) for t, e in (doc.get("positions") or {}).items()}
    candidates = {t.upper(): normalise(t, e) for t, e in (doc.get("candidates") or {}).items()}
    if not positions and not candidates:  # flat mapping
        for t, e in doc.items():
            if isinstance(e, (dict, str)):
                positions[t.upper()] = normalise(t, e)
                candidates[t.upper()] = positions[t.upper()]
    return positions, candidates


def latest_seed_file(settings: Settings) -> Path | None:
    files = sorted(settings.seed_dir.glob("kill_conditions_*.yaml")) + sorted(
        settings.seed_dir.glob("kill_conditions_*.yml")
    )
    return files[-1] if files else None


def candidate_conditions(settings: Settings | None = None) -> dict[str, dict]:
    settings = settings or get_settings()
    path = latest_seed_file(settings)
    return load_file(path)[1] if path else {}


def condition_for(instrument: Instrument, candidates: dict[str, dict]) -> dict | None:
    """Candidate block for an instrument: by ticker first, then by theme."""
    if instrument.ticker.upper() in candidates:
        return candidates[instrument.ticker.upper()]
    for block in candidates.values():
        if block.get("theme") and block["theme"] == instrument.theme:
            return block
    return None


def apply_to_positions(
    session: Session, conditions: dict[str, dict], overwrite: bool = True
) -> int:
    n = 0
    for ticker, block in conditions.items():
        inst = session.exec(select(Instrument).where(Instrument.ticker == ticker)).first()
        if inst is None:
            log.warning("kill condition for unknown ticker %s ignored", ticker)
            continue
        if block.get("theme") and inst.theme != block["theme"]:
            log.warning(
                "%s: kill file theme %r differs from universe theme %r",
                ticker,
                block["theme"],
                inst.theme,
            )
        for row in session.exec(
            select(Position).where(Position.instrument_id == inst.id, Position.closed_at.is_(None))
        ).all():
            if row.kill_json and not overwrite:
                continue
            row.kill_condition = block.get("thesis")
            row.kill_predicate = first_mandatory_predicate(block)
            row.kill_json = block
            session.add(row)
            n += 1
    session.commit()
    return n


def load_seed_kill_conditions(
    session: Session, settings: Settings | None = None, overwrite: bool = True
) -> dict:
    settings = settings or get_settings()
    path = latest_seed_file(settings)
    if path is None:
        return {"status": "missing", "note": "no docs/seed/kill_conditions_*.yaml"}
    positions, candidates = load_file(path)
    n = apply_to_positions(session, positions, overwrite=overwrite)
    return {
        "status": "ok",
        "file": path.name,
        "positions": len(positions),
        "candidates": len(candidates),
        "rows_updated": n,
    }


def kill_block(position: Position) -> dict[str, Any] | None:
    """The canonical block for a position, synthesised from the legacy columns when needed."""
    if position.kill_json:
        return position.kill_json
    if position.kill_predicate or position.kill_condition:
        return {
            "thesis": position.kill_condition,
            "kills": [{"predicate": position.kill_predicate, "severity": "mandatory", "note": None}]
            if position.kill_predicate
            else [],
            "add_blocked_while": None,
            "pre_condition": None,
            "theme": None,
        }
    return None
