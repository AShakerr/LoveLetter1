"""Daily OHLCV for every instrument in the universe via yfinance.

Raw payload shape (JSON-serialisable so it can be cached):
    {"<TICKER>": [{"date": "YYYY-MM-DD", "open":..,"high":..,"low":..,"close":..,"volume":..}, ...]}
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from desk.sources.base import Fetcher, Observation

SOURCE = "yfinance"


def _cell(row, cols: dict[str, str], name: str) -> float | None:
    c = cols.get(name)
    if c is None:
        return None
    v = row[c]
    return None if v != v else float(v)


def frame_to_records(df) -> list[dict[str, Any]]:
    """Convert a yfinance history DataFrame to plain records (pure; used by tests and the fetcher)."""
    out: list[dict[str, Any]] = []
    if df is None or len(df) == 0:
        return out
    cols = {c.lower(): c for c in df.columns}
    for idx, row in df.iterrows():
        d = idx.date() if hasattr(idx, "date") else date.fromisoformat(str(idx)[:10])
        close = row[cols["close"]]
        if close != close:  # NaN
            continue

        out.append(
            {
                "date": d.isoformat(),
                "open": _cell(row, cols, "open"),
                "high": _cell(row, cols, "high"),
                "low": _cell(row, cols, "low"),
                "close": float(close),
                "volume": _cell(row, cols, "volume"),
            }
        )
    return out


class YFinanceFetcher(Fetcher):
    name = SOURCE

    def __init__(
        self,
        symbols: dict[str, str | list[str]],
        settings=None,
        start: date | None = None,
    ) -> None:
        """`symbols` maps display ticker -> yfinance symbol, or a list of candidate symbols tried in order
        (the first one with a non-empty history wins; `_symbols` in the payload records which).
        `start` bounds the history window."""
        super().__init__(settings)
        self.symbols = symbols
        self.start = start or (date.today() - timedelta(days=self.settings.price_lookback_days))

    def _history(self, symbol: str) -> list[dict[str, Any]]:
        import yfinance as yf

        df = yf.Ticker(symbol).history(
            start=self.start.isoformat(), interval="1d", auto_adjust=False, actions=False
        )
        return frame_to_records(df)

    def _raw(self) -> dict[str, list[dict[str, Any]]]:
        raw: dict[str, list[dict[str, Any]]] = {}
        used: dict[str, str] = {}
        errors: list[str] = []
        for ticker, spec in self.symbols.items():
            candidates = [spec] if isinstance(spec, str) else list(spec)
            raw[ticker] = []
            tried: list[str] = []
            for symbol in candidates:
                try:
                    records = self._history(symbol)
                except Exception as exc:  # noqa: BLE001 - one bad symbol must not kill the batch
                    tried.append(f"{symbol}: {exc}")
                    continue
                if records:
                    raw[ticker] = records
                    if symbol != candidates[0]:
                        used[ticker] = symbol
                    break
                tried.append(f"{symbol}: empty history")
            if not raw[ticker]:
                errors.append(f"{ticker} ({'; '.join(tried)})")
        if not raw or all(len(v) == 0 for v in raw.values()):
            raise RuntimeError("yfinance returned no data: " + "; ".join(errors[:5]))
        if errors:
            raw["_errors"] = errors  # type: ignore[assignment]
        if used:
            raw["_symbols"] = used  # type: ignore[assignment]  # fallback symbols that were used
        return raw

    def parse(self, raw: dict[str, Any]) -> list[Observation]:
        obs: list[Observation] = []
        for ticker, records in raw.items():
            if ticker.startswith("_"):
                continue
            for rec in records:
                obs.append(
                    Observation.price(
                        ticker,
                        date.fromisoformat(rec["date"]),
                        rec["close"],
                        source=SOURCE,
                        open=rec.get("open"),
                        high=rec.get("high"),
                        low=rec.get("low"),
                        volume=rec.get("volume"),
                    )
                )
        return obs
