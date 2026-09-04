"""FastAPI app: dashboard (tape + source health), 'Run now', health check. Scheduler runs in-process."""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import select

from desk import __version__
from desk.config import Settings, get_settings
from desk.db import init_db, session_scope
from desk.jobs import run_daily
from desk.models import FetchRun
from desk.scheduler import build_scheduler
from desk.tape import MORE, TAPE, latest_runs, load_tape
from desk.web.auth import BasicAuthMiddleware

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))

_run_lock = threading.Lock()


def _fmt_num(v: float | None, decimals: int = 2) -> str:
    if v is None:
        return "—"
    return f"{v:,.{decimals}f}"


def _fmt_change(v: float | None, mode: str) -> str:
    if v is None or mode == "none":
        return ""
    sign = "+" if v > 0 else ""
    return f"{sign}{v:.2f}%" if mode == "pct" else f"{sign}{v:.2f}"


templates.env.filters["num"] = _fmt_num
templates.env.filters["change"] = _fmt_change


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    settings.ensure_dirs()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_db(settings)
        sched = None
        if settings.scheduler_enabled:
            sched = build_scheduler(settings)
            sched.start()
            app.state.scheduler = sched
            log.info("scheduler started: %s", [str(j.next_run_time) for j in sched.get_jobs()])
        yield
        if sched is not None:
            sched.shutdown(wait=False)

    app = FastAPI(
        title="desk", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None
    )
    app.add_middleware(BasicAuthMiddleware, settings=settings)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    app.state.settings = settings

    def _next_runs(app_: FastAPI) -> list[dict]:
        sched = getattr(app_.state, "scheduler", None)
        if sched is None:
            return []
        return [{"name": j.name, "next": j.next_run_time} for j in sched.get_jobs()]

    def _render_dashboard(request: Request, flash: str | None = None, partial: bool = False):
        with session_scope(settings) as session:
            ctx = {
                "request": request,
                "tape": load_tape(session, TAPE),
                "more": load_tape(session, MORE),
                "runs": latest_runs(session),
                "next_runs": _next_runs(request.app),
                "now": datetime.now(ZoneInfo(settings.tz)),
                "tz": settings.tz,
                "flash": flash,
                "version": __version__,
                "running": _run_lock.locked(),
            }
            name = "partials/dashboard_body.html" if partial else "dashboard.html"
            return templates.TemplateResponse(request, name, ctx)

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "version": __version__}

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        return _render_dashboard(request)

    @app.get("/partials/dashboard", response_class=HTMLResponse)
    def dashboard_partial(request: Request):
        return _render_dashboard(request, partial=True)

    @app.post("/jobs/run", response_class=HTMLResponse)
    def run_now(request: Request):
        if not _run_lock.acquire(blocking=False):
            return _render_dashboard(request, flash="A run is already in progress.", partial=True)
        try:
            summary = run_daily(settings)
        except Exception as exc:  # noqa: BLE001
            log.exception("run now failed")
            return _render_dashboard(request, flash=f"Run failed: {exc}", partial=True)
        finally:
            _run_lock.release()
        ok = sum(1 for s in summary if s["status"] == "ok")
        cached = sum(1 for s in summary if s["status"] == "cached")
        failed = [s["source"] for s in summary if s["status"] == "failed"]
        skipped = [s["source"] for s in summary if s["status"] == "skipped"]
        msg = f"Run finished: {ok} ok, {cached} from cache"
        if failed:
            msg += f", failed: {', '.join(failed)}"
        if skipped:
            msg += f", skipped: {', '.join(skipped)}"
        return _render_dashboard(request, flash=msg, partial=True)

    @app.get("/api/runs")
    def api_runs(limit: int = 50):
        with session_scope(settings) as session:
            rows = session.exec(
                select(FetchRun).order_by(FetchRun.started_at.desc()).limit(limit)
            ).all()
            return JSONResponse([r.model_dump(mode="json") for r in rows])

    @app.get("/api/tape")
    def api_tape():
        with session_scope(settings) as session:
            items = load_tape(session, TAPE + MORE)
            return JSONResponse(
                [
                    {
                        "label": i.spec.label,
                        "key": i.spec.key,
                        "value": i.value,
                        "prev": i.prev,
                        "change": i.change,
                        "as_of": i.as_of.isoformat() if i.as_of else None,
                        "source": i.source,
                        "fetched_at": i.fetched_at.isoformat() if i.fetched_at else None,
                        "freshness": i.freshness,
                    }
                    for i in items
                ]
            )

    return app


def app_factory() -> FastAPI:  # for `uvicorn desk.web.app:app_factory --factory`
    return create_app()
