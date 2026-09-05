"""Record real API payloads into tests/fixtures/ (run on a machine with network + keys in .env).

    uv run python scripts/record_fixtures.py [--days 120] [--only yfinance fred ...] [--screener]

Each fetcher's `_raw()` output is written verbatim as JSON. With --screener it also records the Wikipedia
constituent lists, prices for the screener universe and fundamentals for every stock/ETF, so `desk load-fixtures`
can reproduce the Screener page offline. This replaces the synthetic placeholders from make_synthetic_fixtures.py.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
from datetime import date, timedelta

from desk.config import REPO_ROOT, get_settings
from desk.db import init_db, session_scope
from desk.sources import build_fetchers
from desk.sources.yfinance_source import YFinanceFetcher
from desk.universe import load_universe

OUT = REPO_ROOT / "tests" / "fixtures"
FILES = {
    "yfinance": "yfinance.json",
    "fred": "fred.json",
    "ecb": "ecb.json",
    "alphavantage": "alphavantage.json",
    "gdelt": "gdelt.json",
    "cnn_fear_greed": "fear_greed.json",
    "cftc_cot": "cot.json",
    "cboe": "cboe.json",
    "aaii": "aaii.json",
    "manual": "manual.json",
}


def record_sources(args, settings) -> None:
    for f in build_fetchers(load_universe(), settings):
        if args.only and f.name not in args.only:
            continue
        if isinstance(f, YFinanceFetcher):
            f.start = date.today() - timedelta(days=args.days)
        ok, why = f.enabled()
        if not ok:
            print(f"{f.name}: skipped ({why})")
            continue
        try:
            raw = f._raw()
        except Exception as exc:  # noqa: BLE001
            print(f"{f.name}: FAILED {exc}")
            continue
        path = OUT / FILES[f.name]
        path.write_text(json.dumps(raw, indent=None, default=str), encoding="utf-8")
        print(f"{f.name}: wrote {path} ({len(f.parse(raw))} observations)")


def record_screener(args, settings) -> None:
    from sqlmodel import select

    from desk.fundamentals import fetch_yfinance_info
    from desk.models import Instrument, InstrumentKind
    from desk.screener import (
        fetch_wikipedia_constituents,
        refresh_constituents,
        screener_instruments,
    )

    init_db(settings)
    constituents = {}
    for source in ("sp500", "stoxx600"):
        try:
            constituents[source] = fetch_wikipedia_constituents(source)
            print(f"constituents {source}: {len(constituents[source])} names")
        except Exception as exc:  # noqa: BLE001
            print(f"constituents {source}: FAILED {exc}")
    (OUT / "constituents.json").write_text(json.dumps(constituents, default=str), encoding="utf-8")
    with session_scope(settings) as session:
        refresh_constituents(session, settings, fetch=lambda src: constituents.get(src, []))
        members = screener_instruments(session)
        symbols = {i.ticker: i.source_symbol or i.ticker for i in members}
        f = YFinanceFetcher(
            symbols, settings=settings, start=date.today() - timedelta(days=args.days)
        )
        try:
            raw = f._raw()
            (OUT / "screener_prices.json").write_text(
                json.dumps(raw, default=str), encoding="utf-8"
            )
            print(f"screener prices: {len(raw)} tickers")
        except Exception as exc:  # noqa: BLE001
            print(f"screener prices: FAILED {exc}")
        names = [
            i
            for i in session.exec(select(Instrument)).all()
            if i.kind in (InstrumentKind.stock, InstrumentKind.etf)
            and (i.tradable or i.screener_member)
        ]
        by_ticker = {}
        for i, inst in enumerate(names, 1):
            try:
                by_ticker[inst.ticker] = fetch_yfinance_info(inst.source_symbol or inst.ticker)
            except Exception as exc:  # noqa: BLE001
                print(f"fundamentals {inst.ticker}: FAILED {exc}")
            if i % 50 == 0:
                print(f"fundamentals: {i}/{len(names)}")
        (OUT / "fundamentals.json").write_text(
            json.dumps({"date": dt.date.today().isoformat(), "by_ticker": by_ticker}, default=str),
            encoding="utf-8",
        )
        print(f"fundamentals: {len(by_ticker)} names")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--only", nargs="*")
    ap.add_argument(
        "--screener",
        action="store_true",
        help="also record constituents, screener prices and fundamentals",
    )
    args = ap.parse_args()
    settings = get_settings()
    OUT.mkdir(parents=True, exist_ok=True)
    record_sources(args, settings)
    if args.screener:
        record_screener(args, settings)


if __name__ == "__main__":
    main()
