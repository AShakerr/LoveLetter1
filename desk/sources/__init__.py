"""Fetcher registry. `build_fetchers()` wires each source to the universe/config."""

from __future__ import annotations

from desk.config import Settings, get_settings
from desk.sources.alphavantage import AlphaVantageFetcher
from desk.sources.base import Fetcher, FetchOutcome, Observation
from desk.sources.ecb import EcbFetcher
from desk.sources.fear_greed import FearGreedFetcher
from desk.sources.fred import FredFetcher
from desk.sources.gdelt import GdeltFetcher
from desk.sources.manual import ManualFetcher
from desk.sources.yfinance_source import YFinanceFetcher

__all__ = ["Fetcher", "FetchOutcome", "Observation", "build_fetchers"]


def build_fetchers(universe: list[dict], settings: Settings | None = None) -> list[Fetcher]:
    settings = settings or get_settings()
    symbols = {
        i["ticker"]: i.get("source_symbol") or i["ticker"]
        for i in universe
        if i.get("price_source", "yfinance") == "yfinance"
    }
    sentiment_tickers = [i["ticker"] for i in universe if i.get("news_sentiment")]
    return [
        YFinanceFetcher(symbols, settings=settings),
        FredFetcher(settings=settings),
        EcbFetcher(settings=settings),
        AlphaVantageFetcher(sentiment_tickers, settings=settings),
        GdeltFetcher(settings=settings),
        FearGreedFetcher(settings=settings),
        ManualFetcher(settings=settings),
    ]
