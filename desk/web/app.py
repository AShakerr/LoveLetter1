"""FastAPI app: dashboard (tape + source health), 'Run now', health check. Scheduler runs in-process."""

from __future__ import annotations

import logging
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import markdown as md
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import select

from desk import __version__
from desk.broker.guards import (
    GuardError,
    engage_kill_switch,
    kill_switch_active,
    release_kill_switch,
)
from desk.config import Settings, get_settings
from desk.db import init_db, session_scope
from desk.decisions import list_decisions, respond
from desk.execution import paper_vs_actual, recent_orders
from desk.houseviews import change_log, latest_headline, latest_views
from desk.houseviews import reports as list_reports
from desk.houseviews import risks as list_risks
from desk.ingest.revolut import confirm_batch, discard_batch, pending_batches
from desk.jobs import (
    process_inbox,
    refresh_screener_universe,
    run_daily,
    run_decisions,
    run_screener_daily,
)
from desk.models import Decision, FetchRun, Instrument, RuleFired, Score
from desk.portfolio import build_portfolio
from desk.regime import latest_regime
from desk.rules import RuleConfig
from desk.scheduler import build_scheduler
from desk.screener import page_rows, propose_buy, screener_instruments
from desk.seed import load_all_seeds
from desk.tape import MORE, TAPE, latest_runs, load_tape
from desk.universe import sync_instruments
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


def _fmt_pct(v: float | None, decimals: int = 1) -> str:
    return "—" if v is None else f"{v * 100:.{decimals}f}%"


templates.env.filters["num"] = _fmt_num
templates.env.filters["change"] = _fmt_change
templates.env.filters["pct"] = _fmt_pct
templates.env.filters["markdown"] = lambda text: md.markdown(
    text or "", extensions=["tables", "fenced_code"]
)


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
                "active": "tape",
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
        sources = [s for s in summary if s["source"] not in ("decisions", "screener")]
        dec = next((s for s in summary if s["source"] == "decisions"), None)
        ok = sum(1 for s in sources if s["status"] == "ok")
        cached = sum(1 for s in sources if s["status"] == "cached")
        failed = [s["source"] for s in sources if s["status"] == "failed"]
        skipped = [s["source"] for s in sources if s["status"] == "skipped"]
        msg = f"Run finished: {ok} ok, {cached} from cache"
        if failed:
            msg += f", failed: {', '.join(failed)}"
        if skipped:
            msg += f", skipped: {', '.join(skipped)}"
        if dec is not None:
            msg += (
                f"; decisions: {dec['rows']} new"
                if dec["status"] == "ok"
                else f"; decisions failed: {dec.get('error')}"
            )
        return _render_dashboard(request, flash=msg, partial=True)

    def _base_ctx(request: Request, **extra):
        return {
            "request": request,
            "now": datetime.now(ZoneInfo(settings.tz)),
            "tz": settings.tz,
            "version": __version__,
            "kill": kill_switch_active(settings),
            **extra,
        }

    @app.post("/kill-switch/engage")
    def kill_engage():
        engage_kill_switch(settings)
        return RedirectResponse(
            "/decisions?flash=" + quote_plus("Kill switch engaged: execution halted (data/KILL)"),
            status_code=303,
        )

    @app.post("/kill-switch/release")
    def kill_release(confirm: str = Form("")):
        try:
            release_kill_switch(confirm, settings)
        except GuardError as exc:
            return RedirectResponse("/decisions?flash=" + quote_plus(str(exc)), status_code=303)
        return RedirectResponse(
            "/decisions?flash=" + quote_plus("Kill switch released"), status_code=303
        )

    @app.get("/screener", response_class=HTMLResponse)
    def screener(request: Request, flash: str | None = None):
        with session_scope(settings) as session:
            data = page_rows(session, settings)
            return templates.TemplateResponse(
                request,
                "screener.html",
                _base_ctx(
                    request,
                    data=data,
                    universe_n=len(screener_instruments(session)),
                    flash=flash,
                    active="screener",
                ),
            )

    @app.post("/screener/run")
    def screener_run():
        res = run_screener_daily(settings)
        msg = (
            f"Screener: {res['status']}, scored {res.get('scored', 0)}, wrote {res.get('rows', 0)}"
            + (f" — {res['error']}" if res.get("error") else "")
        )
        return RedirectResponse("/screener?flash=" + quote_plus(msg), status_code=303)

    @app.post("/screener/refresh")
    def screener_refresh():
        res = refresh_screener_universe(settings)
        return RedirectResponse(
            "/screener?flash="
            + quote_plus(
                "Constituents: "
                + "; ".join(
                    f"{k}: {v.get('status')} {v.get('members', v.get('error', ''))}"
                    for k, v in res.items()
                )
            ),
            status_code=303,
        )

    @app.post("/screener/propose")
    def screener_propose(instrument_id: int = Form(...)):
        with session_scope(settings) as session:
            try:
                d = propose_buy(session, instrument_id, settings)
            except ValueError as exc:
                return RedirectResponse("/screener?flash=" + quote_plus(str(exc)), status_code=303)
            did = d.id
        return RedirectResponse(
            f"/decisions/{did}?flash="
            + quote_plus("BUY proposed from the screener; write the thesis before executing"),
            status_code=303,
        )

    @app.get("/portfolio", response_class=HTMLResponse)
    def portfolio(request: Request, flash: str | None = None):
        with session_scope(settings) as session:
            view = build_portfolio(session, settings)
            pend = pending_batches(session)
            instruments = {i.id: i for i in session.exec(select(Instrument)).all()}
            return templates.TemplateResponse(
                request,
                "portfolio.html",
                _base_ctx(
                    request,
                    view=view,
                    pending=pend,
                    instruments=instruments,
                    flash=flash,
                    active="portfolio",
                ),
            )

    @app.post("/positions/confirm")
    def positions_confirm(batch: str = Form(...)):
        with session_scope(settings) as session:
            n = confirm_batch(session, batch)
        return RedirectResponse(f"/portfolio?flash=Confirmed+{n}+positions", status_code=303)

    @app.post("/positions/discard")
    def positions_discard(batch: str = Form(...)):
        with session_scope(settings) as session:
            n = discard_batch(session, batch)
        return RedirectResponse(f"/portfolio?flash=Discarded+{n}+positions", status_code=303)

    @app.get("/house-views", response_class=HTMLResponse)
    def house_views(request: Request, flash: str | None = None):
        with session_scope(settings) as session:
            head_report, headline = latest_headline(session)
            return templates.TemplateResponse(
                request,
                "house_views.html",
                _base_ctx(
                    request,
                    headline=headline,
                    head_report=head_report,
                    views=latest_views(session),
                    tactical=latest_views(session, include_tactical=True),
                    log=change_log(session)[:40],
                    reports=list_reports(session),
                    risks=list_risks(session),
                    flash=flash,
                    active="house",
                ),
            )

    @app.post("/inbox/process")
    def inbox_process(request: Request):
        try:
            result = process_inbox(settings)
        except Exception as exc:  # noqa: BLE001
            log.exception("inbox processing failed")
            msg = f"Inbox failed: {exc}"
        else:
            parts = [
                f"{r.get('file')}: {r.get('status')}"
                for r in result["reports"] + result["portfolio"]
            ]
            msg = "Inbox: " + (", ".join(parts) if parts else "nothing to process")
        back = request.headers.get("referer", "/house-views").split("?")[0] or "/house-views"
        return RedirectResponse(f"{back}?flash={quote_plus(msg)}", status_code=303)

    @app.post("/seed/load")
    def seed_load():
        with session_scope(settings) as session:
            sync_instruments(session)
            result = load_all_seeds(session, settings)
        hv = result.get("house_views") or []
        n = sum(1 for r in hv if r.get("status") == "ok")
        return RedirectResponse(
            f"/house-views?flash={quote_plus(f'Seed loaded: {n} new reports')}", status_code=303
        )

    @app.post("/instruments/confirm-composition")
    def confirm_composition(ticker: str = Form(...)):
        with session_scope(settings) as session:
            inst = session.exec(select(Instrument).where(Instrument.ticker == ticker)).first()
            if inst is not None:
                inst.composition_confirmed = True
                session.add(inst)
                session.commit()
        return RedirectResponse(
            f"/portfolio?flash={quote_plus(f'{ticker}: composition confirmed; limit rules now mandatory')}",
            status_code=303,
        )

    @app.get("/decisions", response_class=HTMLResponse)
    def decisions(
        request: Request,
        date: str | None = None,
        status: str | None = None,
        flash: str | None = None,
    ):
        with session_scope(settings) as session:
            day = datetime.fromisoformat(date).date() if date else None
            rows = list_decisions(session, day, status)
            if day is None and rows:
                day = rows[0].date
                rows = [r for r in rows if r.date == day]
            instruments = {i.id: i for i in session.exec(select(Instrument)).all()}
            regime = latest_regime(session)
            last_run = session.exec(
                select(FetchRun)
                .where(FetchRun.source == "decisions")
                .order_by(FetchRun.started_at.desc())
                .limit(1)
            ).first()
            gflags = [
                r
                for r in session.exec(
                    select(RuleFired).where(
                        RuleFired.date == (day or datetime.now().date()),
                        RuleFired.instrument_id.is_(None),
                    )
                ).all()
            ]
            scores = (
                list(
                    session.exec(
                        select(Score)
                        .where(Score.date == (day or datetime.now().date()))
                        .order_by(Score.total.desc())
                    ).all()
                )
                if day or True
                else []
            )
            decided = {r.instrument_id for r in rows}
            candidates = [
                sc for sc in scores if 60 <= sc.total < 75 and sc.instrument_id not in decided
            ]
            view = build_portfolio(session, settings)
            _actual, _paper, paper = paper_vs_actual(session, settings)
            orders = recent_orders(session, 20)
            dates = sorted({d.date for d in session.exec(select(Decision)).all()}, reverse=True)[
                :30
            ]
            cfg = RuleConfig.load(settings)
            return templates.TemplateResponse(
                request,
                "decisions.html",
                _base_ctx(
                    request,
                    rows=rows,
                    day=day,
                    instruments=instruments,
                    regime=regime,
                    last_run=last_run,
                    gflags=gflags,
                    candidates=candidates,
                    paper=paper,
                    orders=orders,
                    broker_name=settings.broker,
                    dates=dates,
                    view=view,
                    status_filter=status,
                    flash=flash,
                    active="decisions",
                    cfg=cfg,
                ),
            )

    @app.post("/decisions/run")
    def decisions_run():
        res = run_decisions(settings)
        msg = f"Decisions: {res['status']}, {res['rows']} new" + (
            f" — {res['error']}" if res.get("error") else ""
        )
        return RedirectResponse(f"/decisions?flash={quote_plus(msg)}", status_code=303)

    @app.get("/decisions/{decision_id}", response_class=HTMLResponse)
    def decision_detail(request: Request, decision_id: int, flash: str | None = None):
        with session_scope(settings) as session:
            d = session.get(Decision, decision_id)
            if d is None:
                return HTMLResponse("not found", status_code=404)
            inst = session.get(Instrument, d.instrument_id)
            score = session.get(Score, d.score_id) if d.score_id else None
            return templates.TemplateResponse(
                request,
                "decision.html",
                _base_ctx(request, d=d, inst=inst, score=score, flash=flash, active="decisions"),
            )

    @app.post("/decisions/{decision_id}/respond")
    def decision_respond(decision_id: int, status: str = Form(...), note: str = Form("")):
        with session_scope(settings) as session:
            d = session.get(Decision, decision_id)
            if d is None:
                return HTMLResponse("not found", status_code=404)
            try:
                respond(session, d, status, note or None, settings)
            except ValueError as exc:
                return RedirectResponse(
                    f"/decisions/{decision_id}?flash={quote_plus(str(exc))}", status_code=303
                )
        return RedirectResponse(
            f"/decisions/{decision_id}?flash={quote_plus(f'Marked {status}')}", status_code=303
        )

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
