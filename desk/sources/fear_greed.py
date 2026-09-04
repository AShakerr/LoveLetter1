"""CNN Fear & Greed composite. Unofficial endpoint; optional, degrades gracefully.

Raw payload: the endpoint's JSON: {"fear_and_greed": {"score": 55.2, "rating": "neutral",
             "timestamp": "2026-09-03T23:59:59+00:00"}, "fear_and_greed_historical": {"data": [{"x": ms, "y": s}]}}
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from desk.sources.base import Fetcher, Observation, http_get_json, utcnow

SOURCE = "cnn_fear_greed"
SERIES = "CNN_FEAR_GREED"
URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"


class FearGreedFetcher(Fetcher):
    name = SOURCE
    attempts = 2

    def _raw(self) -> dict[str, Any]:
        return http_get_json(
            URL,
            timeout=self.settings.http_timeout_s,
            headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) desk/0.1",
                "Accept": "application/json",
            },
        )

    def parse(self, raw: dict[str, Any]) -> list[Observation]:
        fetched = utcnow()
        by_date: dict[date, tuple[float, str | None]] = {}
        hist = (raw.get("fear_and_greed_historical") or {}).get("data") or []
        for pt in hist:
            d = datetime.fromtimestamp(float(pt["x"]) / 1000, tz=UTC).date()
            by_date[d] = (float(pt["y"]), pt.get("rating"))
        cur = raw.get("fear_and_greed") or {}
        if cur.get("score") is not None:
            ts = cur.get("timestamp")
            d = datetime.fromisoformat(ts).date() if ts else date.today()
            by_date[d] = (float(cur["score"]), cur.get("rating"))
        return [
            Observation(
                series=SERIES,
                date=d,
                value=v,
                source=SOURCE,
                fetched_at=fetched,
                meta={"rating": r},
            )
            for d, (v, r) in sorted(by_date.items())
        ]
