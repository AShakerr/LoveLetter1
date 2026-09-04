"""`desk` command line: init-db, fetch, load-fixtures, backup, serve."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import typer

from desk.config import get_settings

app = typer.Typer(help="desk: private investment decision journal", no_args_is_help=True)


def _log(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


@app.command("init-db")
def init_db_cmd(verbose: bool = False) -> None:
    """Create tables and sync the instrument universe."""
    _log(verbose)
    from desk.db import init_db, session_scope
    from desk.universe import sync_instruments

    settings = get_settings()
    init_db(settings)
    with session_scope(settings) as s:
        n = sync_instruments(s)
    typer.echo(f"db ready at {settings.db_path}; instruments synced ({n} changed)")


@app.command()
def fetch(
    only: list[str] = typer.Option(None, help="Restrict to these source names"),
    verbose: bool = False,
) -> None:
    """Run the daily job now: fetch every source and persist."""
    _log(verbose)
    from desk.jobs import run_daily

    summary = run_daily(get_settings(), only=set(only) if only else None)
    for s in summary:
        typer.echo(f"{s['source']:<16} {s['status']:<8} rows={s['rows']:<5} {s['error'] or ''}")


@app.command("load-fixtures")
def load_fixtures_cmd(
    fixtures_dir: Path = typer.Option(None, help="Defaults to tests/fixtures"),
) -> None:
    """Load the recorded test fixtures into the database (offline demo / development)."""
    _log(False)
    from desk.fixtures import load_fixtures

    summary = load_fixtures(get_settings(), fixtures_dir)
    for s in summary:
        typer.echo(f"{s['source']:<16} {s['status']:<8} rows={s['rows']}")


@app.command()
def seed() -> None:
    """Load docs/seed/ (August house views, positions snapshot, regime snapshot)."""
    _log(False)
    from desk.db import init_db, session_scope
    from desk.seed import load_all_seeds
    from desk.universe import sync_instruments

    settings = get_settings()
    init_db(settings)
    with session_scope(settings) as s:
        sync_instruments(s)
        typer.echo(json.dumps(load_all_seeds(s, settings), indent=1, default=str))


@app.command()
def ingest(verbose: bool = False) -> None:
    """Process inbox/ (Safra PDFs) and inbox/portfolio/ (Revolut screenshots) through Claude."""
    _log(verbose)
    from desk.jobs import process_inbox

    typer.echo(json.dumps(process_inbox(get_settings()), indent=1, default=str))


@app.command()
def positions(
    confirm: str = typer.Option(None, help="Batch id to confirm"),
    discard: str = typer.Option(None, help="Batch id to discard"),
) -> None:
    """List pending position batches, or confirm / discard one."""
    from desk.db import init_db, session_scope
    from desk.ingest.revolut import confirm_batch, discard_batch, pending_batches

    settings = get_settings()
    init_db(settings)
    with session_scope(settings) as s:
        if confirm:
            typer.echo(f"confirmed {confirm_batch(s, confirm)} positions in {confirm}")
        elif discard:
            typer.echo(f"discarded {discard_batch(s, discard)} positions in {discard}")
        else:
            for batch, rows in pending_batches(s).items():
                typer.echo(f"{batch}: {len(rows)} positions pending")


@app.command()
def backup() -> None:
    """Write a consistent SQLite backup to data/backups/."""
    from desk.jobs import backup_sqlite

    typer.echo(str(backup_sqlite(get_settings())))


@app.command()
def serve(host: str = "0.0.0.0", port: int = 8000, reload: bool = False) -> None:
    """Start the dashboard (FastAPI + in-process scheduler)."""
    import uvicorn

    uvicorn.run("desk.web.app:app_factory", factory=True, host=host, port=port, reload=reload)


@app.command()
def tape() -> None:
    """Print the tape as JSON (what the dashboard shows)."""
    from desk.db import init_db, session_scope
    from desk.tape import MORE, TAPE, load_tape

    settings = get_settings()
    init_db(settings)
    with session_scope(settings) as s:
        for i in load_tape(s, TAPE + MORE):
            typer.echo(
                json.dumps(
                    {
                        "label": i.spec.label,
                        "value": i.value,
                        "as_of": str(i.as_of),
                        "source": i.source,
                        "freshness": i.freshness,
                    }
                )
            )


if __name__ == "__main__":
    app()
