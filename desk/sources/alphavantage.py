"""Alpha Vantage NEWS_SENTIMENT. Free tier: 25 calls/day and 1 call/second, so calls are budgeted via a
counter file and paced; a throttle answer is retried once after a pause, then that key is skipped and the
rest of the batch is kept (`_errors` lists what was skipped). The batch itself is never re-run wholesale,
which would burn the daily budget on keys that already succeeded.

Raw payload: {"tickers": {"TSLA": <api json>}, "topics": {"economy_macro": <api json>}}
Each API json has "feed": [{"time_published": "20260903T120000", "overall_sentiment_score": 0.12,
                            "ticker_sentiment": [{"ticker": "TSLA", "ticker_sentiment_score": "0.2"}]}]
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

from desk.sources.base import Fetcher, Observation, http_get_json, utcnow

log = logging.getLogger(__name__)

SOURCE = "alphavantage"
BASE_URL = "https://www.alphavantage.co/query"
DEFAULT_TOPICS = ["economy_macro", "energy_transportation", "financial_markets"]
CALL_SPACING_S = 1.5  # free tier: 1 request per second
THROTTLE_RETRY_S = 3.0


class Throttled(RuntimeError):
    pass


class CallBudget:
    """Persistent per-day call counter so several processes/runs share the free-tier budget."""

    def __init__(self, path: Path, limit: int) -> None:
        self.path, self.limit = path, limit

    def _load(self) -> dict[str, int]:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            if d.get("day") == date.today().isoformat():
                return d
        except (OSError, ValueError):
            pass
        return {"day": date.today().isoformat(), "calls": 0}

    def remaining(self) -> int:
        return max(0, self.limit - self._load()["calls"])

    def consume(self, n: int = 1) -> None:
        d = self._load()
        d["calls"] += n
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(d), encoding="utf-8")


class AlphaVantageFetcher(Fetcher):
    name = SOURCE
    attempts = 1  # per-key handling below; re-running the batch would spend budget on finished keys

    def __init__(
        self,
        tickers: list[str],
        topics: list[str] | None = None,
        settings=None,
        budget: CallBudget | None = None,
        sleep=time.sleep,
    ) -> None:
        super().__init__(settings)
        self.tickers = tickers
        self.topics = topics if topics is not None else DEFAULT_TOPICS
        self.budget = budget or CallBudget(
            self.settings.cache_dir / "alphavantage.budget.json",
            self.settings.alphavantage_daily_budget,
        )
        self._sleep = sleep
        self._last_call: float | None = None

    def enabled(self) -> tuple[bool, str | None]:
        if not self.settings.alphavantage_api_key:
            return False, "ALPHAVANTAGE_API_KEY not set"
        if self.budget.remaining() <= 0:
            return False, "daily call budget exhausted"
        return True, None

    def _call(self, **params: Any) -> Any:
        if self.budget.remaining() <= 0:
            raise RuntimeError("alphavantage daily budget exhausted mid-run")
        if self._last_call is not None:
            self._sleep(CALL_SPACING_S)
        self._last_call = time.monotonic()
        self.budget.consume()
        payload = http_get_json(
            BASE_URL,
            params={
                "function": "NEWS_SENTIMENT",
                "apikey": self.settings.alphavantage_api_key,
                "sort": "LATEST",
                "limit": 50,
                **params,
            },
            timeout=self.settings.http_timeout_s,
        )
        if isinstance(payload, dict) and ("Note" in payload or "Information" in payload):
            raise Throttled(
                f"alphavantage throttled: {payload.get('Note') or payload.get('Information')}"
            )
        return payload

    def _fetch_key(self, label: str, **params: Any) -> Any | None:
        """One key: a throttle answer is retried once after a pause; other failures skip the key."""
        for attempt in (1, 2):
            try:
                return self._call(**params)
            except Throttled as exc:
                if attempt == 1 and self.budget.remaining() > 0:
                    log.warning(
                        "alphavantage %s: throttled; retrying in %.0fs", label, THROTTLE_RETRY_S
                    )
                    self._sleep(THROTTLE_RETRY_S)
                    continue
                self._errors.append(f"{label}: {exc}")
            except Exception as exc:  # noqa: BLE001 - one key must not lose the others
                self._errors.append(f"{label}: {exc}")
            return None
        return None  # pragma: no cover

    def _raw(self) -> dict[str, Any]:
        out: dict[str, Any] = {"tickers": {}, "topics": {}}
        self._errors: list[str] = []
        for t in self.tickers:
            if self.budget.remaining() <= 0:
                self._errors.append(f"{t}: daily budget exhausted")
                continue
            payload = self._fetch_key(t, tickers=t)
            if payload is not None:
                out["tickers"][t] = payload
        for tp in self.topics:
            if self.budget.remaining() <= 0:
                self._errors.append(f"{tp}: daily budget exhausted")
                continue
            payload = self._fetch_key(tp, topics=tp)
            if payload is not None:
                out["topics"][tp] = payload
        if not out["tickers"] and not out["topics"]:
            raise RuntimeError("alphavantage: nothing fetched: " + "; ".join(self._errors))
        if self._errors:
            out["_errors"] = self._errors
        return out

    @staticmethod
    def _pub_date(item: dict[str, Any]) -> date | None:
        tp = item.get("time_published")
        if not tp:
            return None
        return datetime.strptime(tp[:8], "%Y%m%d").date()

    def parse(self, raw: dict[str, Any]) -> list[Observation]:
        fetched = utcnow()
        obs: list[Observation] = []
        # ticker-level: average the ticker-specific score per day
        for ticker, payload in (raw.get("tickers") or {}).items():
            per_day: dict[date, list[float]] = defaultdict(list)
            for item in payload.get("feed", []):
                d = self._pub_date(item)
                if d is None:
                    continue
                for ts in item.get("ticker_sentiment", []):
                    if ts.get("ticker") == ticker and ts.get("ticker_sentiment_score") not in (
                        None,
                        "",
                    ):
                        per_day[d].append(float(ts["ticker_sentiment_score"]))
            for d, scores in per_day.items():
                obs.append(
                    Observation.news(
                        d,
                        sum(scores) / len(scores),
                        source=SOURCE,
                        ticker=ticker,
                        volume=len(scores),
                        fetched_at=fetched,
                    )
                )
        # topic-level: average overall score per day
        for topic, payload in (raw.get("topics") or {}).items():
            per_day = defaultdict(list)
            for item in payload.get("feed", []):
                d = self._pub_date(item)
                s = item.get("overall_sentiment_score")
                if d is None or s is None:
                    continue
                per_day[d].append(float(s))
            for d, scores in per_day.items():
                obs.append(
                    Observation.news(
                        d,
                        sum(scores) / len(scores),
                        source=SOURCE,
                        topic=topic,
                        volume=len(scores),
                        fetched_at=fetched,
                    )
                )
        return obs
