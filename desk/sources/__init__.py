"""Fetcher registry. `build_fetchers()` wires each source to the universe/config."""

from __future__ import annotations

from desk.config import Settings, get_settings
from desk.sources.aaii import AaiiFetcher
from desk.sources.alphavantage import AlphaVantageFetcher
from desk.sources.base import Fetcher, FetchOutcome, Observation
from desk.sources.cot import CotFetcher
from desk.sources.ecb import EcbFetcher
from desk.sources.fear_greed import FearGreedFetcher
from desk.sources.fred import FredFetcher
from desk.sources.gdelt import GdeltFetcher
from desk.sources.manual import ManualFetcher
from desk.sources.yfinance_source import YFinanceFetcher

__all__ = ["Fetcher", "FetchOutcome", "Observation", "build_fetchers", "price_symbols"]


def price_symbols(item: dict) -> str | list[str]:
    """yfinance symbol for a universe entry: `source_symbol`, then `source_symbol_fallbacks` tried in order."""
    primary = item.get("source_symbol") or item["ticker"]
    fallbacks = item.get("source_symbol_fallbacks") or []
    return [primary, *fallbacks] if fallbacks else primary


def build_fetchers(
    universe: list[dict],
    settings: Settings | None = None,
    sentiment_tickers: list[str] | None = None,
) -> list[Fetcher]:
    """`sentiment_tickers` overrides the universe's news_sentiment flags (the daily job passes the budget-ordered
    list from desk.screener.sentiment_targets: held names first, then the screener's top 20)."""
    settings = settings or get_settings()
    symbols = {
        i["ticker"]: price_symbols(i)
        for i in universe
        if i.get("price_source", "yfinance") == "yfinance"
    }
    if sentiment_tickers is None:
        sentiment_tickers = [i["ticker"] for i in universe if i.get("news_sentiment")]
    return [
        YFinanceFetcher(symbols, settings=settings),
        FredFetcher(settings=settings),
        EcbFetcher(settings=settings),
        AlphaVantageFetcher(sentiment_tickers, settings=settings),
        GdeltFetcher(settings=settings),
        FearGreedFetcher(settings=settings),
        CotFetcher(settings=settings),
        AaiiFetcher(settings=settings),
        ManualFetcher(settings=settings),
    ]
