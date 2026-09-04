"""GDELT DOC 2.0 timeline tone + volume for a fixed query set. No key.

Raw payload: {"<query>": {"tone": <timelinetone json>, "volume": <timelinevol json>}}
Each timeline json: {"timeline": [{"series": "...", "data": [{"date": "20260901000000", "value": 1.2}]}]}
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from desk.sources.base import Fetcher, Observation, http_get_json, utcnow

SOURCE = "gdelt"
BASE_URL = "https://api.gdeltproject.org/api/v2/doc/doc"
DEFAULT_QUERIES = [
    "Strait of Hormuz",
    "Federal Reserve rate",
    "ECB",
    "oil price",
    "Egypt IMF",
    "SpaceX",
]


def _timeline(payload: dict[str, Any]) -> dict[date, float]:
    out: dict[date, float] = {}
    for series in payload.get("timeline", []):
        for pt in series.get("data", []):
            d = datetime.strptime(str(pt["date"])[:8], "%Y%m%d").date()
            out[d] = float(pt["value"])
    return out


class GdeltFetcher(Fetcher):
    name = SOURCE

    def __init__(
        self, queries: list[str] | None = None, settings=None, timespan: str = "14d"
    ) -> None:
        super().__init__(settings)
        self.queries = queries or DEFAULT_QUERIES
        self.timespan = timespan

    def _raw(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for q in self.queries:
            query = f'"{q}"' if " " in q else q
            common = {"query": query, "format": "json", "timespan": self.timespan}
            out[q] = {
                "tone": http_get_json(
                    BASE_URL,
                    params={**common, "mode": "timelinetone"},
                    timeout=self.settings.http_timeout_s,
                ),
                "volume": http_get_json(
                    BASE_URL,
                    params={**common, "mode": "timelinevol"},
                    timeout=self.settings.http_timeout_s,
                ),
            }
        return out

    def parse(self, raw: dict[str, Any]) -> list[Observation]:
        fetched = utcnow()
        obs: list[Observation] = []
        for q, payload in raw.items():
            tone = _timeline(payload.get("tone") or {})
            vol = _timeline(payload.get("volume") or {})
            for d, t in tone.items():
                obs.append(
                    Observation.news(
                        d, t, source=SOURCE, topic=q, volume=vol.get(d), fetched_at=fetched
                    )
                )
        return obs
