"""Revolut screenshot -> pending positions (Claude vision). Nothing becomes live until the user confirms
the batch in the UI (docs/BRIEF.md section 3)."""

from __future__ import annotations

import base64
import datetime as dt
import logging
import shutil
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlmodel import Session, select

from desk.config import Settings, get_settings
from desk.llm import TextCompleter, parse_json_text
from desk.models import Instrument, InstrumentKind, Position, Pot
from desk.sources.base import utcnow

log = logging.getLogger(__name__)

MEDIA = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


class ScreenshotPosition(BaseModel):
    model_config = ConfigDict(extra="ignore")
    ticker: str
    name: str | None = None
    pot: str = "brokerage"
    quantity: float | None = None
    last_price: float | None = None
    avg_cost: float | None = (
        None  # explicit when known (cash = 1.0); otherwise backed out from return_pct
    )
    currency: str = "USD"
    value: float | None = None
    return_pct: float | None = None
    note: str | None = None

    @field_validator("pot")
    @classmethod
    def _pot(cls, v: str) -> str:
        v = (v or "brokerage").strip().lower()
        aliases = {
            "robo_advisor": "robo",
            "robo-advisor": "robo",
            "commodity": "commodities",
            "stocks": "brokerage",
        }
        v = aliases.get(v, v)
        if v not in {p.value for p in Pot}:
            raise ValueError(f"pot must be one of brokerage|commodities|robo, got {v!r}")
        return v

    @field_validator("ticker")
    @classmethod
    def _ticker(cls, v: str) -> str:
        return v.strip().upper().replace(" ", "_")

    @field_validator("currency")
    @classmethod
    def _ccy(cls, v: str) -> str:
        return (v or "USD").strip().upper()[:3]


class ScreenshotExtraction(BaseModel):
    model_config = ConfigDict(extra="ignore")
    as_of: dt.date | None = None
    positions: list[ScreenshotPosition] = Field(default_factory=list)
    totals: dict[str, float | None] | None = None
    notes: list[str] = Field(default_factory=list)


def backout_avg_cost(last_price: float | None, return_pct: float | None) -> float | None:
    """Revolut shows return % but not average cost: avg_cost = last_price / (1 + return_pct/100)."""
    if last_price is None:
        return None
    if return_pct is None or return_pct <= -100:
        return last_price
    return last_price / (1 + return_pct / 100)


def extract_screenshot(
    completer: TextCompleter,
    prompt: str,
    image_bytes: bytes,
    media_type: str,
    filename: str = "screenshot.png",
) -> tuple[ScreenshotExtraction | None, Any, str | None]:
    content: list[dict[str, Any]] = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": media_type,
                "data": base64.standard_b64encode(image_bytes).decode("ascii"),
            },
        },
        {"type": "text", "text": "Extract every holding visible in this Revolut screenshot."},
    ]
    last_raw: Any = None
    error: str | None = None
    for attempt in (1, 2):
        try:
            text = completer.complete(prompt, content)
        except Exception as exc:  # noqa: BLE001
            return None, None, f"attempt {attempt}: API error: {type(exc).__name__}: {exc}"
        try:
            last_raw = parse_json_text(text)
            return ScreenshotExtraction.model_validate(last_raw), last_raw, None
        except (ValueError, ValidationError) as exc:
            error = f"attempt {attempt}: {exc}"
            log.warning("%s: %s", filename, error)
            content = content + [
                {
                    "type": "text",
                    "text": "Your previous output failed validation:\n"
                    + error
                    + "\nReturn corrected JSON only.",
                }
            ]
    return None, last_raw, error


def _instrument_for(session: Session, sp: ScreenshotPosition) -> Instrument:
    inst = session.exec(select(Instrument).where(Instrument.ticker == sp.ticker)).first()
    if inst is None:
        kind = InstrumentKind.cash if sp.ticker.startswith("CASH") else InstrumentKind.other
        inst = Instrument(
            ticker=sp.ticker,
            name=sp.name or sp.ticker,
            kind=kind,
            currency=sp.currency,
            tradable=False,
            theme="cash" if kind == InstrumentKind.cash else "unknown (confirm)",
        )
        session.add(inst)
        session.commit()
        session.refresh(inst)
    return inst


def write_pending_positions(
    session: Session,
    positions: list[ScreenshotPosition],
    *,
    batch: str,
    source: str,
    as_of: dt.date,
    confirmed: bool = False,
) -> list[Position]:
    rows: list[Position] = []
    for sp in positions:
        inst = _instrument_for(session, sp)
        qty = sp.quantity
        last_price = sp.last_price
        if qty is None:
            # pot-level line (commodities pot, robo, cash): one unit priced at its value
            qty, last_price = 1.0, sp.value
        elif last_price is None and sp.value is not None and qty:
            last_price = sp.value / qty
        value_native = (
            sp.value
            if sp.value is not None
            else (qty * last_price if last_price is not None else None)
        )
        row = Position(
            instrument_id=inst.id,
            quantity=qty,
            avg_cost=(
                sp.avg_cost
                if sp.avg_cost is not None
                else backout_avg_cost(last_price, sp.return_pct)
                if sp.return_pct is not None
                else 0.0
            ),  # 0.0 = unknown
            currency=sp.currency,
            pot=Pot(sp.pot),
            as_of=as_of,
            confirmed_by_user=confirmed,
            last_price=last_price,
            value_native=value_native,
            return_pct=sp.return_pct,
            source=source,
            batch=batch,
            note=sp.note,
        )
        session.add(row)
        rows.append(row)
    session.commit()
    return rows


def confirm_batch(session: Session, batch: str) -> int:
    """Make a pending batch live: close other open positions for the same instruments, mark confirmed."""
    rows = session.exec(select(Position).where(Position.batch == batch)).all()
    now = utcnow()
    for row in rows:
        others = session.exec(
            select(Position).where(
                Position.instrument_id == row.instrument_id,
                Position.batch != batch,
                Position.closed_at.is_(None),
                Position.confirmed_by_user.is_(True),
            )
        ).all()
        for o in others:
            o.closed_at = now
            session.add(o)
        row.confirmed_by_user = True
        session.add(row)
    session.commit()
    return len(rows)


def discard_batch(session: Session, batch: str) -> int:
    rows = session.exec(
        select(Position).where(Position.batch == batch, Position.confirmed_by_user.is_(False))
    ).all()
    for row in rows:
        session.delete(row)
    session.commit()
    return len(rows)


def pending_batches(session: Session) -> dict[str, list[Position]]:
    rows = session.exec(
        select(Position)
        .where(Position.confirmed_by_user.is_(False), Position.closed_at.is_(None))
        .order_by(Position.id)
    ).all()
    out: dict[str, list[Position]] = {}
    for r in rows:
        out.setdefault(r.batch or "manual", []).append(r)
    return out


def ingest_screenshot(
    session: Session, path: Path, completer: TextCompleter, settings: Settings | None = None
) -> dict[str, Any]:
    settings = settings or get_settings()
    settings.ensure_dirs()
    media = MEDIA.get(path.suffix.lower())
    if media is None:
        return {"file": path.name, "status": "skipped", "error": "unsupported image type"}
    prompt = (settings.prompts_dir / "revolut_extract.md").read_text(encoding="utf-8")
    extraction, raw, error = extract_screenshot(
        completer, prompt, path.read_bytes(), media, path.name
    )
    stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    target = settings.portfolio_archive / f"{stamp}_{path.name}"
    if extraction is None:
        shutil.move(str(path), str(target.with_name(target.stem + "_FAILED" + target.suffix)))
        return {"file": path.name, "status": "failed", "error": error}
    batch = f"screenshot:{stamp}_{path.name}"
    rows = write_pending_positions(
        session,
        extraction.positions,
        batch=batch,
        source="screenshot",
        as_of=extraction.as_of or dt.date.today(),
    )
    shutil.move(str(path), str(target))
    return {
        "file": path.name,
        "status": "pending",
        "batch": batch,
        "positions": len(rows),
        "notes": extraction.notes,
    }


def process_portfolio_inbox(
    session: Session, completer: TextCompleter, settings: Settings | None = None
) -> list[dict[str, Any]]:
    settings = settings or get_settings()
    settings.ensure_dirs()
    out = []
    for path in sorted(p for p in settings.portfolio_inbox.iterdir() if p.suffix.lower() in MEDIA):
        try:
            out.append(ingest_screenshot(session, path, completer, settings))
        except Exception as exc:  # noqa: BLE001
            log.exception("screenshot ingest failed for %s", path.name)
            out.append({"file": path.name, "status": "error", "error": str(exc)})
    return out
