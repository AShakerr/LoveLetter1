"""Valuation factor, redefined (docs/BRIEF.md 7c). Pure functions over a fundamentals dict so the bands are testable."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any

from desk.fundamentals import FINANCIAL_SECTORS


@dataclass
class ValuationResult:
    value: float | None
    inputs: dict[str, Any] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)
    total_cap: float | None = None  # "no earnings" names cannot score above 60 total


def peg_band(peg: float | None) -> tuple[float, str | None]:
    """Below 1.0 -> 5; 1.0-1.5 -> 4; 1.5-2.0 -> 3; 2.0-3.0 -> 2; above 3.0 -> 1; negative/missing -> 0 + flag."""
    if peg is None or peg <= 0:
        return 0.0, "no earnings"
    if peg < 1.0:
        return 5.0, None
    if peg < 1.5:
        return 4.0, None
    if peg < 2.0:
        return 3.0, None
    if peg <= 3.0:
        return 2.0, None
    return 1.0, None


def z_band(z: float) -> float:
    """Below -1 -> 5, -1 to -0.3 -> 4, -0.3 to +0.3 -> 3, +0.3 to +1 -> 2, above +1 -> 1."""
    if z < -1:
        return 5.0
    if z < -0.3:
        return 4.0
    if z <= 0.3:
        return 3.0
    if z <= 1:
        return 2.0
    return 1.0


def zscore(value: float, median: float, std: float) -> float:
    if std and std > 0:
        return (value - median) / std
    # no dispersion known: use relative distance, 15% ~ one band step
    return (value / median - 1) / 0.15 if median else 0.0


def value_trap(f: dict[str, float | None]) -> str | None:
    """Low multiple on collapsing earnings is not cheap."""
    eg, rg = f.get("earningsGrowth"), f.get("revenueGrowth")
    tpe, fpe = f.get("trailingPE"), f.get("forwardPE")
    if (eg is not None and eg < 0) or (rg is not None and rg < 0):
        return "possible value trap: negative earnings or revenue growth"
    if tpe is not None and fpe is not None and 0 < tpe < 8 and fpe > tpe:
        return "possible value trap: P/E below 8 with earnings expected to fall"
    return None


def quality_gate(f: dict[str, float | None], sector: str | None) -> tuple[bool, list[str]]:
    """Screener floor (not a score): FCF > 0, net debt/EBITDA < 3 (financials exempt), revenue growth > 0, >= 5 analysts."""
    reasons = []
    fcf = f.get("freeCashflow")
    if fcf is None or fcf <= 0:
        reasons.append(
            "free cash flow not positive" if fcf is not None else "free cash flow unknown"
        )
    if (sector or "") not in FINANCIAL_SECTORS:
        debt, cash, ebitda = f.get("totalDebt"), f.get("totalCash") or 0.0, f.get("ebitda")
        if debt is None or ebitda is None:
            reasons.append("net debt / EBITDA unknown")
        elif ebitda <= 0 or (debt - cash) / ebitda >= 3:
            reasons.append("net debt / EBITDA not below 3")
    rg = f.get("revenueGrowth")
    if rg is None or rg <= 0:
        reasons.append(
            "revenue growth not positive" if rg is not None else "revenue growth unknown"
        )
    n = f.get("numberOfAnalystOpinions")
    if n is None or n < 5:
        reasons.append("fewer than 5 analyst opinions")
    return not reasons, reasons


def score_stock(
    f: dict[str, float | None],
    sector_stat: dict[str, float] | None,
    own_history: tuple[float | None, int],
    as_of: dict[str, Any] | None = None,
) -> ValuationResult:
    """Three components averaged: PEG, forward P/E vs sector, trailing P/E vs own 5-year median."""
    inputs: dict[str, Any] = {"as_of": as_of or {}}
    flags: list[str] = []
    cap = None
    peg, peg_flag = peg_band(f.get("pegRatio"))
    inputs["peg"] = {"value": f.get("pegRatio"), "score": peg}
    if peg_flag:
        flags.append(peg_flag)
        cap = 60.0
    fpe = f.get("forwardPE")
    if fpe is not None and fpe > 0 and sector_stat and sector_stat.get("median"):
        z = zscore(fpe, sector_stat["median"], sector_stat.get("std", 0.0))
        sector_score = z_band(z)
        inputs["forward_pe_vs_sector"] = {
            "value": fpe,
            "sector_median": sector_stat["median"],
            "sector_std": sector_stat.get("std"),
            "n": sector_stat.get("n"),
            "z": round(z, 3),
            "score": sector_score,
        }
    else:
        sector_score = None
        inputs["forward_pe_vs_sector"] = {
            "value": fpe,
            "note": "no forward P/E or no sector median",
        }
    tpe = f.get("trailingPE")
    median, n_weeks = own_history
    if tpe is not None and tpe > 0 and median and n_weeks >= 52:
        z = zscore(tpe, median, 0.0)
        hist_score = z_band(z)
        inputs["trailing_pe_vs_history"] = {
            "value": tpe,
            "median_5y": median,
            "weeks": n_weeks,
            "z": round(z, 3),
            "score": hist_score,
        }
    else:
        hist_score = sector_score  # until 52 weeks exist, the sector component counts twice
        inputs["trailing_pe_vs_history"] = {
            "value": tpe,
            "median_5y": median,
            "weeks": n_weeks,
            "note": "less than 52 weeks of history: sector component used twice",
        }
    parts = [peg] + [x for x in (sector_score, hist_score) if x is not None]
    if not parts:
        return ValuationResult(None, inputs, flags, cap)
    value = sum(parts) / len(parts)
    trap = value_trap(f)
    if trap:
        flags.append(trap)
        value = min(value, 2.0)
        inputs["value_trap_cap"] = 2.0
    inputs["components"] = {"peg": peg, "sector": sector_score, "history": hist_score}
    return ValuationResult(value, inputs, flags, cap)


def score_etf(
    pe_current: float | None, pe_history: list[tuple[dt.date, float]], fund_pe: float | None = None
) -> ValuationResult:
    """ETFs and indices: P/E vs 5-year median only (index P/E from the Safra tables, fund P/E where yfinance has it)."""
    inputs: dict[str, Any] = {}
    pe = pe_current if pe_current is not None else fund_pe
    if pe is None or not pe_history:
        return ValuationResult(None, {"note": "no P/E series"}, [], None)
    import statistics

    hist = [v for _, v in pe_history if v and v > 0]
    median = statistics.median(hist) if hist else None
    if not median:
        return ValuationResult(None, {"note": "no P/E history"}, [], None)
    z = zscore(pe, median, 0.0)
    value = z_band(z)
    inputs.update(
        {
            "pe": pe,
            "median_5y": median,
            "n": len(hist),
            "z": round(z, 3),
            "fund_pe": fund_pe,
            "note": "history shorter than 5 years" if len(hist) < 60 else None,
        }
    )
    return ValuationResult(value, inputs, [], None)
