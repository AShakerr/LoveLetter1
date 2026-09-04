"""Generate SYNTHETIC placeholder fixtures in the exact shape of each API's raw payload.

These exist only so `pytest` and the offline dashboard work before real payloads are recorded with
scripts/record_fixtures.py. Levels are anchored to the regime snapshot in docs/BRIEF.md section 11
(Brent ~95.8, VIX ~14.3, US CPI 3.4/2.5, EZ HICP 3.3) and are otherwise a seeded random walk.
They are not market data and are never loaded except under source='fixture:*'.
"""

from __future__ import annotations

import json
import random
from datetime import UTC, date, datetime, timedelta

from desk.config import REPO_ROOT

OUT = REPO_ROOT / "tests" / "fixtures"
END = date(2026, 9, 3)
rng = random.Random(20260904)

ANCHORS = {  # last close, daily vol
    "^GSPC": (7450.0, 0.009),
    "^NDX": (27800.0, 0.012),
    "^GDAXI": (25600.0, 0.010),
    "^STOXX50E": (6150.0, 0.010),
    "^VIX": (14.3, 0.05),
    "DXY": (97.6, 0.004),
    "GC=F": (4180.0, 0.009),
    "CL=F": (91.2, 0.02),
    "BZ=F": (95.8, 0.02),
    "HG=F": (4.62, 0.015),
    "SI=F": (52.1, 0.018),
    "BTC-USD": (118400.0, 0.03),
    "ETH-USD": (4650.0, 0.035),
    "EURUSD=X": (1.128, 0.004),
    "EURGBP=X": (0.862, 0.003),
    "TSLA": (352.0, 0.03),
    "NVDA": (188.0, 0.025),
    "VUSA": (126.5, 0.009),
    "VWCE": (142.3, 0.008),
    "X9I1": (7.9, 0.015),
    "4COP": (63.1, 0.02),
    "ORA": (15.97, 0.01),
    "EXW1": (61.3, 0.01),
    "ZPDI": (48.2, 0.012),
}


def trading_days(n: int, end: date = END) -> list[date]:
    days: list[date] = []
    d = end
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return list(reversed(days))


def walk(last: float, vol: float, n: int) -> list[float]:
    vals = [last]
    for _ in range(n - 1):
        vals.append(vals[-1] / (1 + rng.gauss(0, vol)))
    return list(reversed(vals))


def yfinance() -> dict:
    out = {}
    days = trading_days(130)
    for t, (last, vol) in ANCHORS.items():
        closes = walk(last, vol, len(days))
        recs = []
        for d, c in zip(days, closes, strict=True):
            o = c * (1 + rng.gauss(0, vol / 2))
            hi, lo = (
                max(o, c) * (1 + abs(rng.gauss(0, vol / 2))),
                min(o, c) * (1 - abs(rng.gauss(0, vol / 2))),
            )
            v = None if t.endswith("=X") else round(abs(rng.gauss(1, 0.3)) * 1e6)
            recs.append(
                {
                    "date": d.isoformat(),
                    "open": round(o, 4),
                    "high": round(hi, 4),
                    "low": round(lo, 4),
                    "close": round(c, 4),
                    "volume": v,
                }
            )
        out[t] = recs
    return out


def fred() -> dict:
    days = [END - timedelta(days=i) for i in range(120)][::-1]
    daily = {"DFF": 4.33, "DGS2": 4.12, "DGS10": 4.51, "DGS30": 4.98, "T10Y2Y": 0.39}
    out = {}
    for s, last in daily.items():
        obs = []
        v = last - 0.25
        for d in days:
            if d.weekday() >= 5:
                continue
            v += rng.gauss(0.002, 0.02)
            val = (
                "." if d == date(2026, 7, 3) else f"{v:.2f}"
            )  # a holiday row must be skipped by the parser
            obs.append(
                {
                    "realtime_start": END.isoformat(),
                    "realtime_end": END.isoformat(),
                    "date": d.isoformat(),
                    "value": val,
                }
            )
        obs[-1]["value"] = f"{last:.2f}"
        out[s] = {
            "realtime_start": END.isoformat(),
            "realtime_end": END.isoformat(),
            "observation_start": days[0].isoformat(),
            "observation_end": END.isoformat(),
            "units": "lin",
            "count": len(obs),
            "observations": obs,
        }
    monthly = {
        "CPIAUCSL": [329.1, 330.4, 331.9, 333.8],
        "CPILFESL": [326.0, 326.7, 327.4, 328.2],
        "UNRATE": [4.2, 4.2, 4.3, 4.3],
    }
    months = [date(2026, 4, 1), date(2026, 5, 1), date(2026, 6, 1), date(2026, 7, 1)]
    for s, vals in monthly.items():
        out[s] = {
            "units": "lin",
            "count": 4,
            "observations": [
                {
                    "realtime_start": END.isoformat(),
                    "realtime_end": END.isoformat(),
                    "date": m.isoformat(),
                    "value": f"{v}",
                }
                for m, v in zip(months, vals, strict=True)
            ],
        }
    return out


def sdmx(series_key: str, periods: list[str], values: list[float], freq: str) -> dict:
    dims = series_key.split(".")
    return {
        "header": {"id": "synthetic", "prepared": f"{END}T07:00:00", "test": True},
        "dataSets": [
            {
                "action": "Replace",
                "validFrom": f"{END}T07:00:00",
                "series": {
                    ":".join("0" for _ in dims): {
                        "attributes": [],
                        "observations": {str(i): [v, 0, 0] for i, v in enumerate(values)},
                    }
                },
            }
        ],
        "structure": {
            "dimensions": {
                "series": [{"id": "FREQ", "values": [{"id": freq}]}]
                + [{"id": f"D{i}", "values": [{"id": d}]} for i, d in enumerate(dims[1:], 1)],
                "observation": [
                    {
                        "id": "TIME_PERIOD",
                        "role": "time",
                        "values": [{"id": p, "name": p} for p in periods],
                    }
                ],
            }
        },
    }


def ecb() -> dict:
    months = [f"2025-{m:02d}" for m in range(8, 13)] + [f"2026-{m:02d}" for m in range(1, 8)]
    hicp = [2.0, 2.2, 2.1, 2.2, 2.4, 2.3, 2.5, 2.7, 2.9, 3.0, 3.2, 3.3]
    core = [2.3, 2.4, 2.4, 2.3, 2.4, 2.4, 2.4, 2.5, 2.5, 2.5, 2.5, 2.5]
    bdays = [d for d in (END - timedelta(days=i) for i in range(30)) if d.weekday() < 5][::-1]
    dfr = [2.00 if d < date(2026, 6, 11) else 2.25 for d in bdays]
    return {
        "ECB_DEPO": sdmx("B.U2.EUR.4F.KR.DFR.LEV", [d.isoformat() for d in bdays], dfr, "B"),
        "EZ_HICP": sdmx("M.U2.N.000000.4.ANR", months, hicp, "M"),
        "EZ_HICP_CORE": sdmx("M.U2.N.XEF000.4.ANR", months, core, "M"),
    }


def av_feed(ticker: str | None, n: int) -> dict:
    feed = []
    for i in range(n):
        d = END - timedelta(days=i % 6)
        overall = rng.gauss(0.05, 0.2)
        item = {
            "title": f"synthetic headline {i}",
            "url": "https://example.invalid/",
            "time_published": f"{d:%Y%m%d}T{9 + i % 8:02d}0000",
            "source": "synthetic",
            "overall_sentiment_score": round(overall, 4),
            "overall_sentiment_label": "Neutral",
            "ticker_sentiment": [],
        }
        if ticker:
            item["ticker_sentiment"] = [
                {
                    "ticker": ticker,
                    "relevance_score": "0.8",
                    "ticker_sentiment_score": f"{rng.gauss(0.1, 0.25):.4f}",
                    "ticker_sentiment_label": "Somewhat-Bullish",
                }
            ]
        feed.append(item)
    return {"items": str(n), "sentiment_score_definition": "synthetic", "feed": feed}


def alphavantage() -> dict:
    return {
        "tickers": {t: av_feed(t, 18) for t in ("TSLA", "NVDA")},
        "topics": {
            t: av_feed(None, 20)
            for t in ("economy_macro", "energy_transportation", "financial_markets")
        },
    }


def gdelt() -> dict:
    out = {}
    base = {
        "Strait of Hormuz": -4.5,
        "Federal Reserve rate": -1.2,
        "ECB": -0.8,
        "oil price": -2.5,
        "Egypt IMF": -1.5,
        "SpaceX": 1.8,
    }
    days = [END - timedelta(days=i) for i in range(14)][::-1]
    for q, tone in base.items():
        out[q] = {
            "tone": {
                "timeline": [
                    {
                        "series": "Average Tone",
                        "data": [
                            {
                                "date": f"{d:%Y%m%d}000000",
                                "value": round(tone + rng.gauss(0, 0.6), 4),
                            }
                            for d in days
                        ],
                    }
                ]
            },
            "volume": {
                "timeline": [
                    {
                        "series": "Volume Intensity",
                        "data": [
                            {
                                "date": f"{d:%Y%m%d}000000",
                                "value": round(abs(rng.gauss(0.2, 0.1)), 4),
                            }
                            for d in days
                        ],
                    }
                ]
            },
        }
    return out


def fear_greed() -> dict:
    days = [END - timedelta(days=i) for i in range(30)][::-1]
    hist = []
    v = 48.0
    for d in days:
        v = min(95, max(5, v + rng.gauss(0.4, 3)))
        ts = datetime(d.year, d.month, d.day, 23, 59, tzinfo=UTC)
        hist.append(
            {
                "x": ts.timestamp() * 1000,
                "y": round(v, 2),
                "rating": "neutral" if 45 <= v <= 55 else ("greed" if v > 55 else "fear"),
            }
        )
    return {
        "fear_and_greed": {
            "score": hist[-1]["y"],
            "rating": hist[-1]["rating"],
            "timestamp": f"{END}T23:59:59+00:00",
            "previous_close": hist[-2]["y"],
            "previous_1_week": hist[-8]["y"],
        },
        "fear_and_greed_historical": {
            "timestamp": hist[-1]["x"],
            "score": hist[-1]["y"],
            "rating": hist[-1]["rating"],
            "data": hist,
        },
    }


def manual() -> dict:
    return {
        "observations": [
            {
                "series": "EGX30",
                "label": "EGX30 index",
                "value": 33150,
                "as_of": "2026-08-15",
                "unit": "pts",
                "note": "synthetic",
            },
            {
                "series": "CBE_DEPOSIT_RATE",
                "label": "CBE overnight deposit rate",
                "value": 22.25,
                "as_of": "2026-08-15",
                "unit": "%",
                "note": "synthetic",
            },
        ]
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for name, fn in {
        "yfinance": yfinance,
        "fred": fred,
        "ecb": ecb,
        "alphavantage": alphavantage,
        "gdelt": gdelt,
        "fear_greed": fear_greed,
        "manual": manual,
    }.items():
        path = OUT / f"{name}.json"
        path.write_text(json.dumps(fn(), indent=None, separators=(",", ":")), encoding="utf-8")
        print("wrote", path, path.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
