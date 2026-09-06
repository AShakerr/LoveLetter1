"""Replay the recorded fixtures under tests/fixtures/ through the real parsers into the database.

This is the only sanctioned path for non-live numbers (brief: 'no hardcoded market data anywhere
except the fixtures in tests/'). Rows are tagged source='fixture:<source>' so they are never
mistaken for live data.
"""

from __future__ import annotations

import json
from pathlib import Path

from desk.config import REPO_ROOT, Settings
from desk.db import init_db, session_scope
from desk.persist import persist_observations, record_run
from desk.sources.aaii import AaiiFetcher
from desk.sources.alphavantage import AlphaVantageFetcher
from desk.sources.base import Fetcher, FetchOutcome, utcnow
from desk.sources.cot import CotFetcher
from desk.sources.ecb import EcbFetcher
from desk.sources.fear_greed import FearGreedFetcher
from desk.sources.fred import FredFetcher
from desk.sources.gdelt import GdeltFetcher
from desk.sources.manual import ManualFetcher
from desk.sources.yfinance_source import YFinanceFetcher
from desk.universe import load_universe, sync_instruments

DEFAULT_FIXTURES = REPO_ROOT / "tests" / "fixtures"


def fixture_fetchers(settings: Settings) -> list[tuple[Fetcher, str]]:
    return [
        (YFinanceFetcher({}, settings=settings), "yfinance.json"),
        (FredFetcher(settings=settings), "fred.json"),
        (EcbFetcher(settings=settings), "ecb.json"),
        (AlphaVantageFetcher([], settings=settings), "alphavantage.json"),
        (GdeltFetcher(settings=settings), "gdelt.json"),
        (FearGreedFetcher(settings=settings), "fear_greed.json"),
        (CotFetcher(settings=settings), "cot.json"),
        (AaiiFetcher(settings=settings), "aaii.json"),
        (ManualFetcher(settings=settings), "manual.json"),
    ]


def load_fixtures(settings: Settings, fixtures_dir: Path | None = None) -> list[dict]:
    fixtures_dir = fixtures_dir or DEFAULT_FIXTURES
    init_db(settings)
    universe = load_universe(settings.config_dir / "universe.yaml")
    summary: list[dict] = []
    with session_scope(settings) as session:
        sync_instruments(session, universe)
        for fetcher, filename in fixture_fetchers(settings):
            path = fixtures_dir / filename
            if not path.exists():
                summary.append({"source": fetcher.name, "status": "missing", "rows": 0})
                continue
            raw = json.loads(path.read_text(encoding="utf-8"))
            obs = fetcher.parse(raw)
            for o in obs:
                o.source = f"fixture:{o.source}"
            counts = persist_observations(session, obs)
            rows = sum(v for k, v in counts.items() if not k.startswith("skipped"))
            outcome = FetchOutcome(
                fetcher.name, obs, "ok", f"loaded from {path.name}", utcnow(), utcnow()
            )
            record_run(session, outcome, rows)
            summary.append({"source": fetcher.name, "status": "ok", "rows": rows})

        summary.append(
            load_fixture_constituents(session, settings, fixtures_dir / "constituents.json")
        )
        summary.append(
            load_fixture_screener_prices(session, settings, fixtures_dir / "screener_prices.json")
        )
        summary.append(load_fixture_fundamentals(session, fixtures_dir / "fundamentals.json"))
        summary.append(load_fixture_form4(session, fixtures_dir / "form4.json"))
        from desk.events import load_events_config

        summary.append(
            {"source": "events", "status": "ok", "rows": load_events_config(session, settings)}
        )
    return summary


def load_fixture_fundamentals(session, path: Path) -> dict:
    """tests/fixtures/fundamentals.json: {"date": ..., "by_ticker": {TICKER: {field: value, "_sector": ...}}}"""
    import datetime as dt

    from sqlmodel import select

    from desk.fundamentals import store_fundamentals
    from desk.models import Instrument

    if not path.exists():
        return {"source": "fundamentals", "status": "missing", "rows": 0}
    doc = json.loads(path.read_text(encoding="utf-8"))
    on = dt.date.fromisoformat(doc["date"])
    n = 0
    for ticker, values in doc["by_ticker"].items():
        inst = session.exec(select(Instrument).where(Instrument.ticker == ticker)).first()
        if inst is None:
            continue
        n += store_fundamentals(session, inst, on, values, "fixture:yfinance")
    return {"source": "fundamentals", "status": "ok", "rows": n}


def load_fixture_constituents(session, settings, path: Path) -> dict:
    """tests/fixtures/constituents.json: {"sp500": [rows], "stoxx600": [rows]} as fetch_wikipedia_constituents returns."""
    from desk.screener import refresh_constituents

    if not path.exists():
        return {"source": "constituents", "status": "missing", "rows": 0}
    doc = json.loads(path.read_text(encoding="utf-8"))
    res = refresh_constituents(session, settings, fetch=lambda src: doc.get(src, []))
    return {
        "source": "constituents",
        "status": "ok",
        "rows": sum(v.get("members", 0) for v in res.values()),
    }


def load_fixture_screener_prices(session, settings, path: Path) -> dict:
    """Same shape as yfinance.json, for the screener universe."""
    from desk.sources.yfinance_source import YFinanceFetcher

    if not path.exists():
        return {"source": "screener_prices", "status": "missing", "rows": 0}
    raw = json.loads(path.read_text(encoding="utf-8"))
    obs = YFinanceFetcher({}, settings=settings).parse(raw)
    for o in obs:
        o.source = f"fixture:{o.source}"
    counts = persist_observations(session, obs)
    return {
        "source": "screener_prices",
        "status": "ok",
        "rows": sum(v for k, v in counts.items() if not k.startswith("skipped")),
    }


def load_fixture_form4(session, path: Path) -> dict:
    """tests/fixtures/form4.json: the Form4Fetcher raw payload; rows are stored through desk.flow.store_trades
    and the day's signals recomputed."""
    import datetime as dt

    from desk.flow import compute_signals, store_trades
    from desk.sources.form4 import trades_from_raw

    if not path.exists():
        return {"source": "form4", "status": "missing", "rows": 0}
    raw = json.loads(path.read_text(encoding="utf-8"))
    counts = store_trades(session, trades_from_raw(raw))
    as_of = dt.date.fromisoformat(raw["as_of"]) if raw.get("as_of") else dt.date.today()
    compute_signals(session, as_of)
    return {"source": "form4", "status": "ok", "rows": counts["inserted"], "counts": counts}
