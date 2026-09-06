"""Common fetcher contract.

Every source implements `fetch() -> list[Observation]`. Internally that is split into
`_raw()` (the only place that touches the network) and `parse(raw)` (pure), so tests feed
recorded raw payloads to `parse` with the network off.

`run()` adds retry and a last-good cache on disk: if `_raw()` keeps failing, the previous
successful payload is re-parsed and returned flagged `from_cache`, so a dead API never blanks
the dashboard.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from tenacity import RetryError, Retrying, stop_after_attempt, wait_exponential

from desk.config import Settings, get_settings

log = logging.getLogger(__name__)

PRICE_PREFIX = "px:"
NEWS_TICKER_PREFIX = "news:ticker:"
NEWS_TOPIC_PREFIX = "news:topic:"


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@dataclass(slots=True)
class Observation:
    """One dated number. `series` decides which table it lands in:

    - ``px:<TICKER>``          -> prices (meta carries open/high/low/volume)
    - ``news:ticker:<TICKER>`` -> news_sentiment keyed by instrument (meta.volume)
    - ``news:topic:<topic>``   -> news_sentiment keyed by topic (meta.volume)
    - anything else            -> observations
    """

    series: str
    date: date
    value: float
    source: str
    fetched_at: datetime = field(default_factory=utcnow)
    meta: dict[str, Any] | None = None

    # --- constructors -------------------------------------------------------------
    @classmethod
    def price(
        cls,
        ticker: str,
        d: date,
        close: float,
        *,
        source: str,
        open: float | None = None,
        high: float | None = None,
        low: float | None = None,
        volume: float | None = None,
        fetched_at: datetime | None = None,
    ) -> Observation:
        return cls(
            series=f"{PRICE_PREFIX}{ticker}",
            date=d,
            value=float(close),
            source=source,
            fetched_at=fetched_at or utcnow(),
            meta={"open": open, "high": high, "low": low, "volume": volume},
        )

    @classmethod
    def news(
        cls,
        d: date,
        score: float,
        *,
        source: str,
        ticker: str | None = None,
        topic: str | None = None,
        volume: float | None = None,
        fetched_at: datetime | None = None,
    ) -> Observation:
        if (ticker is None) == (topic is None):
            raise ValueError("news observation needs exactly one of ticker/topic")
        series = f"{NEWS_TICKER_PREFIX}{ticker}" if ticker else f"{NEWS_TOPIC_PREFIX}{topic}"
        return cls(
            series=series,
            date=d,
            value=float(score),
            source=source,
            fetched_at=fetched_at or utcnow(),
            meta={"volume": volume},
        )

    # --- classification ------------------------------------------------------------
    @property
    def is_price(self) -> bool:
        return self.series.startswith(PRICE_PREFIX)

    @property
    def is_news(self) -> bool:
        return self.series.startswith(NEWS_TICKER_PREFIX) or self.series.startswith(
            NEWS_TOPIC_PREFIX
        )

    @property
    def ticker(self) -> str | None:
        if self.is_price:
            return self.series[len(PRICE_PREFIX) :]
        if self.series.startswith(NEWS_TICKER_PREFIX):
            return self.series[len(NEWS_TICKER_PREFIX) :]
        return None

    @property
    def topic(self) -> str | None:
        if self.series.startswith(NEWS_TOPIC_PREFIX):
            return self.series[len(NEWS_TOPIC_PREFIX) :]
        return None

    # --- (de)serialisation for the last-good cache ------------------------------------
    def to_json(self) -> dict[str, Any]:
        return {
            "series": self.series,
            "date": self.date.isoformat(),
            "value": self.value,
            "source": self.source,
            "fetched_at": self.fetched_at.isoformat(),
            "meta": self.meta,
        }

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Observation:
        return cls(
            series=d["series"],
            date=date.fromisoformat(d["date"]),
            value=float(d["value"]),
            source=d["source"],
            fetched_at=datetime.fromisoformat(d["fetched_at"]),
            meta=d.get("meta"),
        )


@dataclass(slots=True)
class FetchOutcome:
    source: str
    observations: list[Observation]
    status: str  # ok | cached | failed | skipped
    error: str | None = None
    started_at: datetime = field(default_factory=utcnow)
    finished_at: datetime | None = None
    raw: Any = (
        None  # the payload behind the observations (fetchers whose rows are not time series use it)
    )


class Fetcher(ABC):
    """Base class. Subclasses set `name`, implement `_raw()` and `parse()`."""

    name: str = "base"
    attempts: int = 3

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    # -- contract -------------------------------------------------------------------
    @abstractmethod
    def _raw(self) -> Any:
        """Hit the network and return a JSON-serialisable payload."""

    @abstractmethod
    def parse(self, raw: Any) -> list[Observation]:
        """Pure: turn a raw payload into observations."""

    def enabled(self) -> tuple[bool, str | None]:
        """Return (False, reason) when a fetcher cannot run (e.g. missing API key)."""
        return True, None

    def fetch(self) -> list[Observation]:
        return self.parse(self._raw())

    # -- retry + last-good cache -----------------------------------------------------
    @property
    def cache_path(self) -> Path:
        return self.settings.cache_dir / f"{self.name}.last_good.json"

    def _write_cache(self, raw: Any) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(
                json.dumps({"saved_at": utcnow().isoformat(), "raw": raw}, default=str),
                encoding="utf-8",
            )
        except (OSError, TypeError, ValueError) as exc:  # pragma: no cover - best effort
            log.warning("%s: could not write last-good cache: %s", self.name, exc)

    def _read_cache(self) -> Any | None:
        try:
            if self.cache_path.exists():
                return json.loads(self.cache_path.read_text(encoding="utf-8"))["raw"]
        except (OSError, ValueError, KeyError) as exc:  # pragma: no cover - best effort
            log.warning("%s: could not read last-good cache: %s", self.name, exc)
        return None

    def run(self) -> FetchOutcome:
        started = utcnow()
        ok, reason = self.enabled()
        if ok and getattr(self.settings, "offline", False):
            ok, reason = False, "offline (DESK_OFFLINE=1)"
        if not ok:
            return FetchOutcome(self.name, [], "skipped", reason, started, utcnow())
        try:
            for attempt in Retrying(
                stop=stop_after_attempt(self.attempts),
                wait=wait_exponential(multiplier=1, min=1, max=10),
                reraise=False,
            ):
                with attempt:
                    raw = self._raw()
        except RetryError as exc:
            cause = exc.last_attempt.exception()
            err = f"{type(cause).__name__}: {cause}"
            log.warning("%s: fetch failed after %d attempts: %s", self.name, self.attempts, err)
            cached = self._read_cache()
            if cached is None:
                return FetchOutcome(self.name, [], "failed", err, started, utcnow())
            try:
                obs = self.parse(cached)
            except Exception as pexc:  # noqa: BLE001
                return FetchOutcome(
                    self.name, [], "failed", f"{err}; cache unparseable: {pexc}", started, utcnow()
                )
            return FetchOutcome(self.name, obs, "cached", err, started, utcnow(), raw=cached)
        try:
            obs = self.parse(raw)
        except Exception as exc:  # noqa: BLE001
            err = f"parse error: {type(exc).__name__}: {exc}"
            log.exception("%s: %s", self.name, err)
            return FetchOutcome(self.name, [], "failed", err, started, utcnow())
        self._write_cache(raw)
        return FetchOutcome(self.name, obs, "ok", None, started, utcnow(), raw=raw)


def http_get_json(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: float = 20.0,
    headers: dict[str, str] | None = None,
) -> Any:
    import httpx

    with httpx.Client(
        timeout=timeout, headers=headers or {"User-Agent": "desk/0.1"}, follow_redirects=True
    ) as client:
        r = client.get(url, params=params)
        r.raise_for_status()
        return r.json()
