"""Safra Sarasin PDF -> house_views (docs/BRIEF.md section 5).

1. Hash the file; skip if reports.sha256 exists.
2. Send the PDF to Claude with prompts/safra_extract.md. Ask for JSON only. Validate against the Pydantic
   schema; on failure retry once with the validation error appended; on a second failure store
   raw_json = null and flag the report.
3. Write house_views rows. For each view, find the most recent prior view with the same scope + key and
   populate changed_from, so upgrades and downgrades are first-class events.
4. Move the file to archive/reports/YYYY-MM-DD_<kind>.pdf.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import logging
import shutil
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlmodel import Session, select

from desk.config import Settings, get_settings
from desk.llm import TextCompleter, parse_json_text
from desk.models import HouseView, Report
from desk.sources.base import utcnow

log = logging.getLogger(__name__)

STANCES = {
    "most_preferred",
    "neutral",
    "least_preferred",
    "overweight",
    "underweight",
    "buy",
    "hold",
    "sell",
    "strong_buy",
}
SCOPES = {"region", "sector", "asset", "index_target", "rate", "fx", "commodity", "stock"}
KINDS = {"cross_asset_weekly", "economic_outlook", "equity_focus_list", "market_views", "other"}
# Tactical grids use a different vocabulary and horizon than the strategy documents. The seed data says:
# "Treat the strategy document as the house view; the tactical grid may lag." So changed_from is resolved
# within a tier, and the house view shown to the scorer comes from the strategic tier.
TACTICAL_KINDS = {"market_views"}

# Rank so that vocabulary differences (most_preferred vs overweight) are not reported as changes.
STANCE_RANK = {
    "least_preferred": 0,
    "underweight": 0,
    "sell": 0,
    "neutral": 1,
    "hold": 1,
    "most_preferred": 2,
    "overweight": 2,
    "buy": 2,
    "strong_buy": 3,
}


class View(BaseModel):
    model_config = ConfigDict(extra="ignore")
    scope: str
    key: str
    stance: str | None = None
    value: str | None = None
    changed_from: str | None = None
    quote: str | None = None
    page: int | None = None

    @field_validator("scope")
    @classmethod
    def _scope(cls, v: str) -> str:
        if v not in SCOPES:
            raise ValueError(f"scope must be one of {sorted(SCOPES)}, got {v!r}")
        return v

    @field_validator("stance")
    @classmethod
    def _stance(cls, v: str | None) -> str | None:
        if v in (None, "", "null"):
            return None
        v = v.strip().lower().replace(" ", "_")
        if v not in STANCES:
            raise ValueError(f"stance must be one of {sorted(STANCES)} or null, got {v!r}")
        return v

    @field_validator("value", "changed_from", mode="before")
    @classmethod
    def _stringify(cls, v: Any) -> str | None:
        if v is None:
            return None
        return str(v)

    @field_validator("quote")
    @classmethod
    def _quote(cls, v: str | None) -> str | None:
        return v[:400] if v else v


class Risk(BaseModel):
    model_config = ConfigDict(extra="ignore")
    risk: str
    quote: str | None = None
    page: int | None = None


class SafraExtraction(BaseModel):
    model_config = ConfigDict(extra="ignore")
    publisher: str = "Safra Sarasin"
    kind: str
    date: dt.date
    headline_stance: dict[str, str | None] | None = None
    views: list[View] = Field(default_factory=list)
    risks: list[Risk] = Field(default_factory=list)
    market_performance_table: list[dict[str, Any]] = Field(default_factory=list)
    forecast_tables: dict[str, Any] | None = None
    inconsistencies: list[str] = Field(default_factory=list)

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        v = v.strip().lower()
        return v if v in KINDS else "other"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_prompt(settings: Settings, name: str = "safra_extract.md") -> str:
    return (settings.prompts_dir / name).read_text(encoding="utf-8")


def validate_extraction(text: str) -> SafraExtraction:
    return SafraExtraction.model_validate(parse_json_text(text))


def extract_with_retry(
    completer: TextCompleter, prompt: str, pdf_bytes: bytes, filename: str = "report.pdf"
) -> tuple[SafraExtraction | None, Any, str | None]:
    """Returns (extraction or None, last raw JSON-ish payload or None, error)."""
    doc = {
        "type": "document",
        "source": {
            "type": "base64",
            "media_type": "application/pdf",
            "data": base64.standard_b64encode(pdf_bytes).decode("ascii"),
        },
        "title": filename,
    }
    content: list[dict[str, Any]] = [
        doc,
        {"type": "text", "text": "Extract the house view from this report."},
    ]
    last_raw: Any = None
    error: str | None = None
    for attempt in (1, 2):
        try:
            text = completer.complete(prompt, content)
        except Exception as exc:  # noqa: BLE001
            error = f"attempt {attempt}: API error: {type(exc).__name__}: {exc}"
            log.warning("%s: %s", filename, error)
            break
        try:
            last_raw = parse_json_text(text)
        except (ValueError, json.JSONDecodeError) as exc:
            last_raw = {"_unparseable_text": text[:20000]}
            error = f"attempt {attempt}: not valid JSON: {exc}"
        else:
            try:
                return SafraExtraction.model_validate(last_raw), last_raw, None
            except ValidationError as exc:
                error = f"attempt {attempt}: schema validation failed: {exc}"
        log.warning("%s: %s", filename, error)
        content = content + [
            {
                "type": "text",
                "text": (
                    "Your previous output failed validation with this error:\n"
                    + error
                    + "\nReturn the corrected JSON only, matching the schema exactly. No prose."
                ),
            }
        ]
    return None, last_raw, error


def _prior_view(
    session: Session, report: Report, scope: str, key: str, tactical: bool
) -> HouseView | None:
    stmt = (
        select(HouseView, Report)
        .join(Report, HouseView.report_id == Report.id)
        .where(
            HouseView.scope == scope,
            HouseView.key == key,
            Report.id != report.id,
            Report.date <= report.date,
        )
        .order_by(Report.date.desc(), Report.id.desc())
    )
    for hv, rep in session.exec(stmt).all():
        if (rep.kind in TACTICAL_KINDS) == tactical:
            return hv
    return None


def resolve_changed_from(view: View, prior: HouseView | None) -> str | None:
    """Explicit changed_from from the report wins. Otherwise diff against the prior view of the same key."""
    if view.changed_from:
        return view.changed_from
    if prior is None:
        return None
    if view.stance is not None and prior.stance is not None:
        if STANCE_RANK.get(view.stance) != STANCE_RANK.get(prior.stance):
            return prior.stance
        return None
    if (
        view.stance is None
        and prior.stance is None
        and view.value is not None
        and prior.value is not None
    ):
        if view.value.strip() != prior.value.strip():
            return prior.value
    return None


def write_report(
    session: Session,
    extraction: SafraExtraction | None,
    *,
    filename: str,
    sha256: str,
    raw_json: Any,
    error: str | None = None,
    publisher: str = "Safra Sarasin",
    kind: str = "other",
    date: dt.date | None = None,
) -> Report:
    if extraction is not None:
        publisher, kind, date = extraction.publisher, extraction.kind, extraction.date
    report = Report(
        publisher=publisher,
        kind=kind,
        date=date or dt.date.today(),
        filename=filename,
        sha256=sha256,
        extracted_at=utcnow(),
        raw_json=raw_json if extraction is not None else None,
        flagged=extraction is None,
        flag_reason=error,
    )
    session.add(report)
    session.commit()
    session.refresh(report)
    if extraction is None:
        return report
    tactical = report.kind in TACTICAL_KINDS
    for v in extraction.views:
        prior = _prior_view(session, report, v.scope, v.key, tactical)
        session.add(
            HouseView(
                report_id=report.id,
                scope=v.scope,
                key=v.key,
                stance=v.stance,
                value=v.value,
                changed_from=resolve_changed_from(v, prior),
                quote=v.quote,
                page=v.page,
            )
        )
    session.commit()
    return report


def archive_path(settings: Settings, report: Report, suffix: str = ".pdf") -> Path:
    base = f"{report.date.isoformat()}_{report.kind}"
    target = settings.reports_archive / f"{base}{suffix}"
    n = 1
    while target.exists():
        n += 1
        target = settings.reports_archive / f"{base}-{n}{suffix}"
    return target


def ingest_pdf(
    session: Session, path: Path, completer: TextCompleter, settings: Settings | None = None
) -> Report | None:
    """Full pipeline for one file. Returns None when the file was already ingested (moved anyway)."""
    settings = settings or get_settings()
    settings.ensure_dirs()
    digest = sha256_file(path)
    existing = session.exec(select(Report).where(Report.sha256 == digest)).first()
    if existing is not None:
        log.info("%s: already ingested as report %s, moving to archive", path.name, existing.id)
        shutil.move(str(path), str(archive_path(settings, existing, suffix=f"_dup{path.suffix}")))
        return None
    extraction, raw, error = extract_with_retry(
        completer, load_prompt(settings), path.read_bytes(), path.name
    )
    report = write_report(
        session, extraction, filename=path.name, sha256=digest, raw_json=raw, error=error
    )
    shutil.move(
        str(path), str(archive_path(settings, report, suffix=path.suffix.lower() or ".pdf"))
    )
    return report


def process_reports_inbox(
    session: Session, completer: TextCompleter, settings: Settings | None = None
) -> list[dict[str, Any]]:
    settings = settings or get_settings()
    settings.ensure_dirs()
    out: list[dict[str, Any]] = []
    for path in sorted(settings.reports_inbox.glob("*.pdf")) + sorted(
        settings.reports_inbox.glob("*.PDF")
    ):
        try:
            report = ingest_pdf(session, path, completer, settings)
        except Exception as exc:  # noqa: BLE001
            log.exception("ingest failed for %s", path.name)
            out.append({"file": path.name, "status": "error", "error": str(exc)})
            continue
        if report is None:
            out.append({"file": path.name, "status": "duplicate"})
        else:
            out.append(
                {
                    "file": path.name,
                    "status": "flagged" if report.flagged else "ok",
                    "report_id": report.id,
                    "kind": report.kind,
                    "date": report.date.isoformat(),
                    "error": report.flag_reason,
                }
            )
    return out
