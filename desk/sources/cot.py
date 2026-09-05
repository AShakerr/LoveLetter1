"""CFTC Commitments of Traders, legacy futures-only, from the yearly zip on cftc.gov.

Raw payload: {"2026": "<csv text>", ...}. Net speculative position = noncommercial long - short, stored as
observations COT:GOLD, COT:CRUDE, COT:COPPER, COT:SP500, COT:EUR, COT:TNOTE10 (one row per report date)."""

from __future__ import annotations

import csv
import datetime as dt
import io
import zipfile
from typing import Any

from desk.sources.base import Fetcher, Observation

SOURCE = "cftc_cot"
URL = "https://www.cftc.gov/files/dea/history/deacot{year}.zip"
MARKETS = {
    "COT:GOLD": "GOLD - COMMODITY EXCHANGE INC.",
    "COT:CRUDE": "CRUDE OIL, LIGHT SWEET - NEW YORK MERCANTILE EXCHANGE",
    "COT:COPPER": "COPPER- #1 - COMMODITY EXCHANGE INC.",
    "COT:SP500": "E-MINI S&P 500 - CHICAGO MERCANTILE EXCHANGE",
    "COT:EUR": "EURO FX - CHICAGO MERCANTILE EXCHANGE",
    "COT:TNOTE10": "10-YEAR U.S. TREASURY NOTES - CHICAGO BOARD OF TRADE",
}
COL_NAME = "Market and Exchange Names"
COL_DATE = "As of Date in Form YYYY-MM-DD"
COL_LONG = "Noncommercial Positions-Long (All)"
COL_SHORT = "Noncommercial Positions-Short (All)"


class CotFetcher(Fetcher):
    name = SOURCE
    attempts = 2

    def __init__(self, years: int = 3, settings=None) -> None:
        super().__init__(settings)
        self.years = years

    def _raw(self) -> dict[str, str]:
        import httpx

        out: dict[str, str] = {}
        this_year = dt.date.today().year
        with httpx.Client(
            timeout=60, follow_redirects=True, headers={"User-Agent": "desk/0.1"}
        ) as client:
            for year in range(this_year - self.years + 1, this_year + 1):
                r = client.get(URL.format(year=year))
                r.raise_for_status()
                with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
                    name = next(
                        n
                        for n in zf.namelist()
                        if n.lower().endswith(".txt") or n.lower().endswith(".csv")
                    )
                    out[str(year)] = zf.read(name).decode("utf-8", errors="replace")
        return out

    def parse(self, raw: dict[str, Any]) -> list[Observation]:
        wanted = {v.upper(): k for k, v in MARKETS.items()}
        obs: list[Observation] = []
        for _year, text in raw.items():
            reader = csv.DictReader(io.StringIO(text))
            for row in reader:
                series = wanted.get((row.get(COL_NAME) or "").strip().upper())
                if series is None:
                    continue
                try:
                    d = dt.date.fromisoformat(row[COL_DATE].strip())
                    net = float(row[COL_LONG]) - float(row[COL_SHORT])
                except (KeyError, ValueError, AttributeError):
                    continue
                obs.append(
                    Observation(
                        series=series,
                        date=d,
                        value=net,
                        source=SOURCE,
                        meta={"long": float(row[COL_LONG]), "short": float(row[COL_SHORT])},
                    )
                )
        return obs
