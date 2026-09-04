"""Retry, last-good cache fallback, skip-when-unconfigured, and the Alpha Vantage budget."""

from datetime import date
from pathlib import Path

import pytest

from desk.sources.alphavantage import AlphaVantageFetcher, CallBudget
from desk.sources.base import Fetcher, Observation
from desk.sources.fred import FredFetcher


class Flaky(Fetcher):
    name = "flaky"
    attempts = 3

    def __init__(self, settings, fail_times: int, payload=None):
        super().__init__(settings)
        self.fail_times, self.calls, self.payload = fail_times, 0, payload or {"v": 1.5}

    def _raw(self):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ConnectionError("boom")
        return self.payload

    def parse(self, raw):
        return [Observation(series="X", date=date(2026, 9, 3), value=raw["v"], source=self.name)]


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    monkeypatch.setattr("desk.sources.base.wait_exponential", lambda **_: lambda *_a, **_k: 0)


def test_retry_then_success(settings):
    f = Flaky(settings, fail_times=2)
    out = f.run()
    assert out.status == "ok" and f.calls == 3 and out.observations[0].value == 1.5
    assert f.cache_path.exists()


def test_falls_back_to_last_good_cache(settings):
    ok = Flaky(settings, fail_times=0, payload={"v": 2.0})
    assert ok.run().status == "ok"
    dead = Flaky(settings, fail_times=99)
    out = dead.run()
    assert out.status == "cached" and out.observations[0].value == 2.0
    assert "ConnectionError" in (out.error or "")


def test_failed_with_no_cache(settings):
    out = Flaky(settings, fail_times=99).run()
    assert out.status == "failed" and out.observations == []


def test_parse_error_is_failed_not_cached(settings):
    class BadParse(Flaky):
        name = "badparse"

        def parse(self, raw):
            raise ValueError("nope")

    out = BadParse(settings, fail_times=0).run()
    assert out.status == "failed" and "parse error" in out.error


def test_fred_skipped_without_key(settings):
    out = FredFetcher(settings=settings).run()
    assert out.status == "skipped" and "FRED_API_KEY" in out.error


def test_alphavantage_budget(settings, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(settings, "alphavantage_api_key", "k")
    budget = CallBudget(tmp_path / "b.json", limit=2)
    f = AlphaVantageFetcher(["TSLA", "NVDA", "AAPL"], topics=[], settings=settings, budget=budget)
    calls = []
    monkeypatch.setattr(
        "desk.sources.alphavantage.http_get_json",
        lambda url, params=None, **kw: (calls.append(params["tickers"]), {"feed": []})[1],
    )
    with pytest.raises(RuntimeError, match="budget"):
        f._raw()
    assert calls == ["TSLA", "NVDA"] and budget.remaining() == 0
    assert f.enabled() == (False, "daily call budget exhausted")
