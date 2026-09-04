"""Alpha Vantage NEWS_SENTIMENT. Free tier: 25 calls/day, so calls are budgeted via a counter file.

Raw payload: {"tickers": {"TSLA": <api json>}, "topics": {"economy_macro": <api json>}}
Each API json has "feed": [{"time_published": "20260903T120000", "overall_sentiment_score": 0.12,
                            "ticker_sentiment": [{"ticker": "TSLA", "ticker_sentiment_score": "0.2"}]}]
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

from desk.sources.base import Fetcher, Observation, http_get_json, utcnow

SOURCE = "alphavantage"
BASE_URL = "https://www.alphavantage.co/query"
DEFAULT_TOPICS = ["economy_macro", "energy_transportation", "financial_markets"]


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

    def __init__(
        self,
        tickers: list[str],
        topics: list[str] | None = None,
        settings=None,
        budget: CallBudget | None = None,
    ) -> None:
        super().__init__(settings)
        self.tickers = tickers
        self.topics = topics if topics is not None else DEFAULT_TOPICS
        self.budget = budget or CallBudget(
            self.settings.cache_dir / "alphavantage.budget.json",
            self.settings.alphavantage_daily_budget,
        )

    def enabled(self) -> tuple[bool, str | None]:
        if not self.settings.alphavantage_api_key:
            return False, "ALPHAVANTAGE_API_KEY not set"
        if self.budget.remaining() <= 0:
            return False, "daily call budget exhausted"
        return True, None

    def _call(self, **params: Any) -> Any:
        if self.budget.remaining() <= 0:
            raise RuntimeError("alphavantage daily budget exhausted mid-run")
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
            raise RuntimeError(
                f"alphavantage throttled: {payload.get('Note') or payload.get('Information')}"
            )
        return payload

    def _raw(self) -> dict[str, Any]:
        out: dict[str, Any] = {"tickers": {}, "topics": {}}
        for t in self.tickers:
            out["tickers"][t] = self._call(tickers=t)
        for tp in self.topics:
            out["topics"][tp] = self._call(topics=tp)
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
