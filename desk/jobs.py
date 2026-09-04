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
    return summary


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
