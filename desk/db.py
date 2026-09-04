"""Engine and session helpers. One SQLite file under the data dir."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from desk import models  # noqa: F401  (registers tables on SQLModel.metadata)
from desk.config import Settings, get_settings

_engine: Engine | None = None
_engine_url: str | None = None


def get_engine(settings: Settings | None = None) -> Engine:
    global _engine, _engine_url
    settings = settings or get_settings()
    if _engine is None or _engine_url != settings.db_url:
        settings.ensure_dirs()
        _engine = create_engine(settings.db_url, connect_args={"check_same_thread": False})

        @event.listens_for(_engine, "connect")
        def _pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        _engine_url = settings.db_url
    return _engine


def reset_engine() -> None:
    global _engine, _engine_url
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _engine_url = None


def init_db(settings: Settings | None = None) -> None:
    SQLModel.metadata.create_all(get_engine(settings))


@contextmanager
def session_scope(settings: Settings | None = None) -> Iterator[Session]:
    with Session(get_engine(settings)) as session:
        yield session
