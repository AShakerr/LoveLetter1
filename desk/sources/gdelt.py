"""GDELT DOC 2.0 timeline tone + volume for a fixed query set. No key.

GDELT rate-limits bursts (HTTP 429). Each query gets its own exponential backoff and there is a pause between
requests; a query that still fails after its retries is recorded under `_errors` and the other queries are
kept. The source only fails when every query failed.

Raw payload: {"<query>": {"tone": <timelinetone json>, "volume": <timelinevol json>}, "_errors": [...]}
Each timeline json: {"timeline": [{"series": "...", "data": [{"date": "20260901000000", "value": 1.2}]}]}
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime
from typing import Any

from desk.sources.base import Fetcher, Observation, http_get_json, utcnow

log = logging.getLogger(__name__)

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
REQUEST_DELAY_S = 1.5  # pause between consecutive requests
BACKOFF_S = (2.0, 4.0, 8.0)  # waits after the 1st, 2nd, 3rd failed attempt of one request


def _timeline(payload: dict[str, Any]) -> dict[date, float]:
    out: dict[date, float] = {}
    for series in payload.get("timeline", []):
        for pt in series.get("data", []):
            d = datetime.strptime(str(pt["date"])[:8], "%Y%m%d").date()
            out[d] = float(pt["value"])
    return out


def _status(exc: Exception) -> int | None:
    resp = getattr(exc, "response", None)
    return getattr(resp, "status_code", None)


class GdeltFetcher(Fetcher):
    name = SOURCE
    attempts = 1  # retries happen per request below, not around the whole batch

    def __init__(
        self,
        queries: list[str] | None = None,
        settings=None,
        timespan: str = "14d",
        sleep=time.sleep,
    ) -> None:
        super().__init__(settings)
        self.queries = queries or DEFAULT_QUERIES
        self.timespan = timespan
        self._sleep = sleep

    def _get(self, params: dict[str, Any]) -> Any:
        """One request with exponential backoff on 429/5xx and on transport errors."""
        last: Exception | None = None
        for i, wait in enumerate((*BACKOFF_S, None)):
            try:
                return http_get_json(BASE_URL, params=params, timeout=self.settings.http_timeout_s)
            except Exception as exc:  # noqa: BLE001 - classified below
                last = exc
                code = _status(exc)
                retryable = code is None or code == 429 or code >= 500
                if not retryable or wait is None:
                    raise
                log.warning(
                    "gdelt %s: %s; retry %d in %.0fs", params.get("query"), code or exc, i + 1, wait
                )
                self._sleep(wait)
        raise last  # pragma: no cover - loop always returns or raises

    def _raw(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        errors: list[str] = []
        first = True
        for q in self.queries:
            query = f'"{q}"' if " " in q else q
            common = {"query": query, "format": "json", "timespan": self.timespan}
            try:
                payload: dict[str, Any] = {}
                for mode in ("timelinetone", "timelinevol"):
                    if not first:
                        self._sleep(REQUEST_DELAY_S)
                    first = False
                    payload[mode] = self._get({**common, "mode": mode})
                out[q] = {"tone": payload["timelinetone"], "volume": payload["timelinevol"]}
            except Exception as exc:  # noqa: BLE001 - one query must not fail the source
                errors.append(f"{q}: {_status(exc) or type(exc).__name__}: {exc}")
        if not out:
            raise RuntimeError("gdelt: every query failed: " + "; ".join(errors))
        if errors:
            out["_errors"] = errors
        return out

    def parse(self, raw: dict[str, Any]) -> list[Observation]:
        fetched = utcnow()
        obs: list[Observation] = []
        for q, payload in raw.items():
            if q.startswith("_") or not isinstance(payload, dict):
                continue
            tone = _timeline(payload.get("tone") or {})
            vol = _timeline(payload.get("volume") or {})
            for d, t in tone.items():
                obs.append(
                    Observation.news(
                        d, t, source=SOURCE, topic=q, volume=vol.get(d), fetched_at=fetched
                    )
                )
        return obs
