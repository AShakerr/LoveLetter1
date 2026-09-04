"""Record real API payloads into tests/fixtures/ (run on a machine with network + keys in .env).

    uv run python scripts/record_fixtures.py [--days 120]

Each fetcher's `_raw()` output is written verbatim as JSON. This replaces the synthetic placeholders
produced by scripts/make_synthetic_fixtures.py.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, timedelta

from desk.config import REPO_ROOT, get_settings
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
    "manual": "manual.json",
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=120)
    ap.add_argument("--only", nargs="*")
    args = ap.parse_args()
    settings = get_settings()
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
        path.write_text(json.dumps(raw, indent=1, default=str), encoding="utf-8")
        print(f"{f.name}: wrote {path} ({len(f.parse(raw))} observations)")


if __name__ == "__main__":
    main()
