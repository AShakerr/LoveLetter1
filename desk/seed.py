"""Load docs/seed/ into the database: August house views, the 4 Sep positions snapshot, the regime snapshot.

House views go through the same write path as PDF extraction so changed_from resolves identically."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from sqlmodel import Session, select

from desk.config import Settings, get_settings
from desk.ingest.revolut import ScreenshotPosition, write_pending_positions
from desk.ingest.safra import SafraExtraction, write_report
from desk.models import Instrument, Position, Regime, Report

log = logging.getLogger(__name__)


def _digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def load_seed_house_views(session: Session, path: Path) -> list[dict[str, Any]]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for rep in sorted(doc["reports"], key=lambda r: (r["date"], r.get("kind", ""))):
        digest = _digest(rep)
        if session.exec(select(Report).where(Report.sha256 == digest)).first() is not None:
            out.append({"file": rep.get("filename"), "status": "already loaded"})
            continue
        extraction = SafraExtraction.model_validate(rep)
        report = write_report(
            session,
            extraction,
            filename=rep.get("filename") or f"seed_{rep['date']}.json",
            sha256=digest,
            raw_json=rep,
        )
        out.append(
            {
                "file": report.filename,
                "status": "ok",
                "report_id": report.id,
                "views": len(extraction.views),
            }
        )
    return out


def load_seed_positions(session: Session, path: Path) -> dict[str, Any]:
    """Load the snapshot as a pending batch. If the batch already exists (the file grew), only the tickers
    not yet in the batch are added, as pending rows the user confirms on the Portfolio page."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    meta = doc.get("_meta", {})
    as_of = dt.date.fromisoformat(meta.get("as_of") or dt.date.today().isoformat())
    batch = f"seed:{path.stem}"
    ids = meta.get("identifiers", {})
    existing = session.exec(select(Position).where(Position.batch == batch)).all()
    have = set()
    if existing:
        inst_by_id = {i.id: i.ticker for i in session.exec(select(Instrument)).all()}
        have = {inst_by_id.get(r.instrument_id) for r in existing}
    positions = []
    for p in doc["positions"]:
        if p["ticker"] in have:
            continue
        ident = ids.get(p["ticker"], {})
        positions.append(
            ScreenshotPosition(
                ticker=p["ticker"],
                name=ident.get("name"),
                pot=p.get("pot", "brokerage"),
                quantity=p.get("quantity"),
                avg_cost=p.get("avg_cost"),
                last_price=p.get("last_price")
                if p.get("last_price") is not None
                else p.get("avg_cost"),
                currency=p.get("currency", "USD"),
                value=p.get("value_usd") if p.get("quantity") is None else None,
                return_pct=p.get("return_pct"),
                note=p.get("note"),
            )
        )
    if not positions:
        return {"batch": batch, "status": "already loaded"}
    rows = write_pending_positions(
        session,
        positions,
        batch=batch,
        source="seed",
        as_of=as_of,
        confirmed=bool(meta.get("confirmed_by_user", False)),
    )
    return {
        "batch": batch,
        "status": "extended" if existing else "pending",
        "positions": len(rows),
        "as_of": as_of.isoformat(),
    }


def load_seed_regime(session: Session, path: Path) -> dict[str, Any]:
    doc = json.loads(path.read_text(encoding="utf-8"))
    d = dt.date.fromisoformat(doc["date"])
    row = session.exec(select(Regime).where(Regime.date == d)).first()
    if row is None:
        row = Regime(
            date=d,
            label=doc["label"],
            inflation_state=doc["inflation_state"],
            policy_state=doc["policy_state"],
            oil_state=doc["oil_state"],
            vol_state=doc["vol_state"],
            inputs_json=doc.get("inputs_json"),
        )
        session.add(row)
        session.commit()
        return {"date": doc["date"], "status": "ok"}
    return {"date": doc["date"], "status": "already loaded"}


def load_all_seeds(session: Session, settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    d = settings.seed_dir
    out: dict[str, Any] = {}
    hv = d / "house_views_2026-08.json"
    if hv.exists():
        out["house_views"] = load_seed_house_views(session, hv)
    pos = sorted(d.glob("positions_*.json"))
    if pos:
        out["positions"] = load_seed_positions(session, pos[-1])
    reg = sorted(d.glob("regime_*.json"))
    if reg:
        out["regime"] = load_seed_regime(session, reg[-1])
    from desk.kill_conditions import load_seed_kill_conditions

    out["kill_conditions"] = load_seed_kill_conditions(session, settings)
    return out
