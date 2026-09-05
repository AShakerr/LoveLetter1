"""CBOE total put/call ratio. Optional: the CSV location changes; degrade gracefully.

Raw payload: {"csv": "<text>"} with columns DATE, ..., "P/C Ratio" or "TOTAL PUT/CALL RATIO"."""

from __future__ import annotations

import csv
import datetime as dt
import io
from typing import Any

from desk.sources.base import Fetcher, Observation

SOURCE = "cboe"
SERIES = "CBOE_PUTCALL_TOTAL"
URL = "https://cdn.cboe.com/data/us/options/market_statistics/daily/totalpc.csv"


def _parse_date(s: str) -> dt.date | None:
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d-%b-%y"):
        try:
            return dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


class CboeFetcher(Fetcher):
    name = SOURCE
    attempts = 2

    def _raw(self) -> dict[str, str]:
        import httpx

        with httpx.Client(
            timeout=self.settings.http_timeout_s,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 desk/0.1"},
        ) as client:
            r = client.get(URL)
            r.raise_for_status()
            return {"csv": r.text}

    def parse(self, raw: dict[str, Any]) -> list[Observation]:
        text = raw.get("csv") or ""
        lines = [ln for ln in text.splitlines() if "," in ln]
        # skip preamble lines until the header that contains DATE
        start = next((i for i, ln in enumerate(lines) if "DATE" in ln.upper()), 0)
        reader = csv.DictReader(io.StringIO("\n".join(lines[start:])))
        obs = []
        for row in reader:
            keys = {k.strip().upper(): v for k, v in row.items() if k}
            d = _parse_date(keys.get("DATE", ""))
            ratio = (
                keys.get("P/C RATIO")
                or keys.get("TOTAL PUT/CALL RATIO")
                or keys.get("PUT/CALL RATIO")
            )
            if d is None or ratio in (None, ""):
                continue
            try:
                obs.append(Observation(series=SERIES, date=d, value=float(ratio), source=SOURCE))
            except ValueError:
                continue
        return obs
