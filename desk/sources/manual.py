"""Hand-maintained observations (Egypt: EGX30, CBE rate...) from config/manual_observations.yaml.

The dashboard paints these red when the as-of date is older than 14 days. Rows whose note contains
PLACEHOLDER are skipped: a placeholder zero is not an observation and must never enter a series.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import yaml

from desk.sources.base import Fetcher, Observation, utcnow

SOURCE = "manual"
STALE_AFTER_DAYS = 14


class ManualFetcher(Fetcher):
    name = SOURCE
    attempts = 1

    def __init__(self, path: Path | None = None, settings=None) -> None:
        super().__init__(settings)
        self.path = path or (self.settings.config_dir / "manual_observations.yaml")

    def _raw(self) -> dict[str, Any]:
        with open(self.path, encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}

    def parse(self, raw: dict[str, Any]) -> list[Observation]:
        fetched = utcnow()
        obs: list[Observation] = []
        for item in raw.get("observations") or []:
            if "PLACEHOLDER" in str(item.get("note") or "").upper():
                continue
            as_of = item["as_of"]
            d = as_of if isinstance(as_of, date) else date.fromisoformat(str(as_of))
            obs.append(
                Observation(
                    series=item["series"],
                    date=d,
                    value=float(item["value"]),
                    source=SOURCE,
                    fetched_at=fetched,
                    meta={
                        "note": item.get("note"),
                        "unit": item.get("unit"),
                        "label": item.get("label"),
                    },
                )
            )
        return obs
