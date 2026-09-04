"""The daily job and the nightly backup. Both are plain functions so the CLI, the scheduler and the
'Run now' button call the same code."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from desk.config import Settings, get_settings
from desk.db import init_db, session_scope
from desk.persist import persist_observations, record_run
from desk.sources import build_fetchers
from desk.sources.base import Fetcher
from desk.universe import load_universe, sync_instruments

log = logging.getLogger(__name__)


def run_daily(
    settings: Settings | None = None,
    fetchers: list[Fetcher] | None = None,
    only: set[str] | None = None,
    decide: bool = True,
) -> list[dict]:
    """Fetch every source, persist, and log a fetch_runs row per source. Returns a summary list."""
    settings = settings or get_settings()
    init_db(settings)
    universe = load_universe(settings.config_dir / "universe.yaml")
    fetchers = fetchers if fetchers is not None else build_fetchers(universe, settings)
    summary: list[dict] = []
    with session_scope(settings) as session:
        sync_instruments(session, universe)
        for f in fetchers:
            if only and f.name not in only:
                continue
            outcome = f.run()
            counts = (
                persist_observations(session, outcome.observations) if outcome.observations else {}
            )
            rows = sum(v for k, v in counts.items() if not k.startswith("skipped"))
            record_run(session, outcome, rows)
            summary.append(
                {
                    "source": f.name,
                    "status": outcome.status,
                    "rows": rows,
                    "observations": len(outcome.observations),
                    "error": outcome.error,
                    "counts": dict(counts),
                }
            )
            log.info("%s: %s (%d rows) %s", f.name, outcome.status, rows, outcome.error or "")
    if decide:
        summary.append(run_decisions(settings))
    return summary


def run_decisions(settings: Settings | None = None) -> dict:
    """Regime -> scores -> rules -> decisions -> paper broker. Logged as a fetch_runs row named 'decisions'."""
    from desk.decisions import run_pipeline
    from desk.models import FetchRun

    settings = settings or get_settings()
    init_db(settings)
    started = datetime.utcnow()
    with session_scope(settings) as session:
        try:
            res = run_pipeline(session, settings)
        except Exception as exc:  # noqa: BLE001
            log.exception("decision pipeline failed")
            session.add(
                FetchRun(
                    source="decisions",
                    started_at=started,
                    finished_at=datetime.utcnow(),
                    status="failed",
                    rows=0,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            session.commit()
            return {
                "source": "decisions",
                "status": "failed",
                "rows": 0,
                "observations": 0,
                "error": str(exc),
                "counts": {},
            }
        note = "; ".join(res.notes) or None
        session.add(
            FetchRun(
                source="decisions",
                started_at=started,
                finished_at=datetime.utcnow(),
                status="ok",
                rows=res.created,
                error=note,
            )
        )
        session.commit()
        return {
            "source": "decisions",
            "status": "ok",
            "rows": res.created,
            "observations": len(res.scores),
            "error": note,
            "counts": {
                "decisions": res.created,
                "flags": len(res.flags),
                "paper_fills": res.paper_fills,
            },
        }


def process_inbox(settings: Settings | None = None, completer=None) -> dict[str, list[dict]]:
    """Ingest every PDF in inbox/ and every screenshot in inbox/portfolio/. Needs ANTHROPIC_API_KEY."""
    from desk.ingest.revolut import process_portfolio_inbox
    from desk.ingest.safra import process_reports_inbox
    from desk.llm import get_completer

    settings = settings or get_settings()
    settings.ensure_dirs()
    has_files = (
        any(settings.reports_inbox.glob("*.pdf"))
        or any(settings.reports_inbox.glob("*.PDF"))
        or any(
            p
            for p in settings.portfolio_inbox.iterdir()
            if p.is_file() and not p.name.startswith(".")
        )
    )
    if not has_files:
        return {"reports": [], "portfolio": []}
    completer = completer or get_completer(settings)
    init_db(settings)
    with session_scope(settings) as session:
        return {
            "reports": process_reports_inbox(session, completer, settings),
            "portfolio": process_portfolio_inbox(session, completer, settings),
        }


def scan_inbox(settings: Settings | None = None) -> None:
    """Scheduler entry point: silent when the inbox is empty or the API key is missing."""
    settings = settings or get_settings()
    try:
        result = process_inbox(settings)
    except RuntimeError as exc:  # no API key
        log.warning("inbox scan skipped: %s", exc)
        return
    for kind, items in result.items():
        for it in items:
            log.info("inbox %s: %s", kind, it)


def backup_sqlite(settings: Settings | None = None) -> Path:
    """Consistent online backup via sqlite3's backup API; keeps the newest N files."""
    settings = settings or get_settings()
    settings.ensure_dirs()
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    target = settings.backup_dir / f"desk_{stamp}.sqlite3"
    n = 1
    while target.exists():  # several backups within one second must not overwrite each other
        n += 1
        target = settings.backup_dir / f"desk_{stamp}-{n}.sqlite3"
    src = sqlite3.connect(str(settings.db_path))
    dst = sqlite3.connect(str(target))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    backups = sorted(settings.backup_dir.glob("desk_*.sqlite3"), key=lambda p: p.stat().st_mtime_ns)
    for old in backups[: -settings.backups_to_keep]:
        old.unlink(missing_ok=True)
    return target
