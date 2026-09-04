"""f_regime (docs/BRIEF.md section 7, amended): a static table in config/regime_fit.yaml maps
(theme, regime dimension, state) to a 0-5 score.

    fit(regime) = mean of the four dimension scores for the instrument's theme
    f_regime    = 0.6 * fit(current regime) + 0.4 * fit(reverse scenario)

The table is the user's and is not invented here. Until config/regime_fit.yaml exists, f_regime is None and
every score carries the flag "regime_fit.yaml missing". The loader accepts this shape:

    reverse_scenario:            # optional; defaults to REVERSE below
      inflation_state: contained
      policy_state: cutting
      oil_state: normal
      vol_state: normal
    themes:
      gold:
        inflation_state: {energy_shock: 4, broad: 4, contained: 2}
        policy_state:    {hiking: 2, on_hold: 3, cutting: 4}
        oil_state:       {shock: 3, elevated: 3, normal: 3}
        vol_state:       {complacent: 2, normal: 3, stressed: 4}
      default: {...}             # optional fallback for themes not listed
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from desk.models import Regime

DIMENSIONS = ("inflation_state", "policy_state", "oil_state", "vol_state")

# Default reverse scenario: the world flipping on each dimension.
REVERSE = {
    "inflation_state": {"energy_shock": "contained", "broad": "contained", "contained": "broad"},
    "policy_state": {"hiking": "cutting", "cutting": "hiking", "on_hold": "hiking"},
    "oil_state": {"shock": "normal", "elevated": "normal", "normal": "shock"},
    "vol_state": {"complacent": "stressed", "stressed": "complacent", "normal": "stressed"},
}


@dataclass
class RegimeFit:
    themes: dict[str, dict[str, dict[str, float]]]
    reverse_scenario: dict[str, str] | None = None
    path: Path | None = None
    notes: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> RegimeFit | None:
        if not path.exists():
            return None
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        themes = doc.get("themes") or {
            k: v for k, v in doc.items() if isinstance(v, dict) and k != "reverse_scenario"
        }
        return cls(
            themes={
                str(t): {
                    d: {str(s): float(x) for s, x in (dims.get(d) or {}).items()}
                    for d in DIMENSIONS
                }
                for t, dims in themes.items()
            },
            reverse_scenario=doc.get("reverse_scenario"),
            path=path,
        )

    def reverse_of(self, regime: Regime) -> dict[str, str]:
        if self.reverse_scenario:
            return {d: self.reverse_scenario.get(d, getattr(regime, d)) for d in DIMENSIONS}
        return {d: REVERSE[d].get(getattr(regime, d), getattr(regime, d)) for d in DIMENSIONS}

    def fit(self, theme: str | None, states: dict[str, str]) -> tuple[float | None, dict[str, Any]]:
        table = self.themes.get(theme or "") or self.themes.get("default")
        if table is None:
            return None, {"note": f"theme {theme!r} not in regime_fit.yaml and no default"}
        scores = {}
        for d in DIMENSIONS:
            st = states.get(d)
            val = table.get(d, {}).get(st)
            if val is not None:
                scores[d] = val
        if not scores:
            return None, {"note": "no dimension scored", "states": states}
        return sum(scores.values()) / len(scores), {"states": states, "scores": scores}

    def score(self, theme: str | None, regime: Regime) -> tuple[float | None, dict[str, Any]]:
        current = {d: getattr(regime, d) for d in DIMENSIONS}
        reverse = self.reverse_of(regime)
        f_cur, i_cur = self.fit(theme, current)
        f_rev, i_rev = self.fit(theme, reverse)
        if f_cur is None or f_rev is None:
            return None, {"current": i_cur, "reverse": i_rev}
        return 0.6 * f_cur + 0.4 * f_rev, {
            "current": i_cur,
            "reverse": i_rev,
            "fit_current": round(f_cur, 3),
            "fit_reverse": round(f_rev, 3),
            "formula": "0.6*current + 0.4*reverse",
        }
