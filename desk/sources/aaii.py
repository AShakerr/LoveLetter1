"""AAII investor sentiment survey (weekly). Optional scrape of the public page; the manual observation
AAII_BULL_BEAR_SPREAD in config/manual_observations.yaml is the fallback.

Raw payload: {"html": "..."}; parse finds the latest Bullish / Bearish percentages."""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from desk.sources.base import Fetcher, Observation

SOURCE = "aaii"
SERIES = "AAII_BULL_BEAR_SPREAD"
URL = "https://www.aaii.com/sentimentsurvey"
_PCT = re.compile(r"(Bullish|Bearish)[^0-9]{0,80}?(\d{1,2}(?:\.\d)?)\s*%", re.I | re.S)
_DATE = re.compile(r"(?:week ending|Week Ending|as of)\s*([A-Z][a-z]+ \d{1,2},? \d{4})")


class AaiiFetcher(Fetcher):
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
            return {"html": r.text}

    def parse(self, raw: dict[str, Any]) -> list[Observation]:
        html = raw.get("html") or ""
        found = {m.group(1).lower(): float(m.group(2)) for m in _PCT.finditer(html)}
        if "bullish" not in found or "bearish" not in found:
            return []
        d = dt.date.today()
        m = _DATE.search(html)
        if m:
            for fmt in ("%B %d, %Y", "%B %d %Y"):
                try:
                    d = dt.datetime.strptime(m.group(1).replace(",", ","), fmt).date()
                    break
                except ValueError:
                    continue
        return [
            Observation(
                series=SERIES,
                date=d,
                value=found["bullish"] - found["bearish"],
                source=SOURCE,
                meta={"bullish": found["bullish"], "bearish": found["bearish"]},
            )
        ]
