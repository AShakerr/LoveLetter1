"""FRED series. Raw payload: {"<SERIES>": {"observations": [{"date": "...", "value": "4.33"}, ...]}}"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from desk.sources.base import Fetcher, Observation, http_get_json

SOURCE = "fred"
BASE_URL = "https://api.stlouisfed.org/fred/series/observations"
DEFAULT_SERIES = ["DFF", "DGS2", "DGS10", "DGS30", "CPIAUCSL", "CPILFESL", "T10Y2Y", "UNRATE"]


MIN_LOOKBACK_DAYS = 480


class FredFetcher(Fetcher):
    name = SOURCE

    def __init__(
        self, series: list[str] | None = None, settings=None, start: date | None = None
    ) -> None:
        super().__init__(settings)
        self.series = series or DEFAULT_SERIES
        # CPI y/y needs 13 monthly prints, so the window is at least MIN_LOOKBACK_DAYS whatever the price lookback
        days = max(self.settings.price_lookback_days, MIN_LOOKBACK_DAYS)
        self.start = start or (date.today() - timedelta(days=days))

    def enabled(self) -> tuple[bool, str | None]:
        if not self.settings.fred_api_key:
            return False, "FRED_API_KEY not set"
        return True, None

    def _raw(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for s in self.series:
            out[s] = http_get_json(
                BASE_URL,
                params={
                    "series_id": s,
                    "api_key": self.settings.fred_api_key,
                    "file_type": "json",
                    "observation_start": self.start.isoformat(),
                },
                timeout=self.settings.http_timeout_s,
            )
        return out

    def parse(self, raw: dict[str, Any]) -> list[Observation]:
        obs: list[Observation] = []
        for series, payload in raw.items():
            for rec in payload.get("observations", []):
                v = rec.get("value")
                if v in (None, "", "."):
                    continue
                obs.append(
                    Observation(
                        series=series,
                        date=date.fromisoformat(rec["date"]),
                        value=float(v),
                        source=SOURCE,
                    )
                )
        return obs
