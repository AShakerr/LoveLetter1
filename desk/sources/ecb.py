"""ECB Data Portal (SDMX-JSON). Raw payload: {"<OUR_SERIES_NAME>": <sdmx json>}

Series mapping (ours -> ECB key):
    ECB_DEPO        FM.B.U2.EUR.4F.KR.DFR.LEV        deposit facility rate, business daily
    EZ_HICP        ICP.M.U2.N.000000.4.ANR          headline HICP, y/y %
    EZ_HICP_CORE   ICP.M.U2.N.XEF000.4.ANR          HICP ex energy & food, y/y %
"""

from __future__ import annotations

from datetime import date
from typing import Any

from desk.sources.base import Fetcher, Observation, http_get_json

SOURCE = "ecb"
BASE_URL = "https://data-api.ecb.europa.eu/service/data"
SERIES = {
    "ECB_DEPO": "FM/B.U2.EUR.4F.KR.DFR.LEV",
    "EZ_HICP": "ICP/M.U2.N.000000.4.ANR",
    "EZ_HICP_CORE": "ICP/M.U2.N.XEF000.4.ANR",
}


def period_to_date(period: str) -> date:
    """'2026-07' -> 2026-07-01, '2026-09-02' -> that day, '2026' -> Jan 1."""
    parts = period.split("-")
    if len(parts) == 1:
        return date(int(parts[0]), 1, 1)
    if len(parts) == 2:
        return date(int(parts[0]), int(parts[1]), 1)
    return date.fromisoformat(period[:10])


def parse_sdmx(payload: dict[str, Any]) -> list[tuple[str, float]]:
    """Return [(period, value)] from an SDMX-JSON message with a single series."""
    out: list[tuple[str, float]] = []
    datasets = payload.get("dataSets") or []
    if not datasets:
        return out
    obs_dims = payload["structure"]["dimensions"]["observation"]
    time_values = [v["id"] for v in obs_dims[0]["values"]]
    for series in datasets[0].get("series", {}).values():
        for idx, vals in series.get("observations", {}).items():
            v = vals[0] if vals else None
            if v is None:
                continue
            out.append((time_values[int(idx)], float(v)))
    return out


class EcbFetcher(Fetcher):
    name = SOURCE

    def __init__(
        self, series: dict[str, str] | None = None, settings=None, last_n: int = 36
    ) -> None:
        super().__init__(settings)
        self.series = series or SERIES
        self.last_n = last_n

    def _raw(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for ours, key in self.series.items():
            out[ours] = http_get_json(
                f"{BASE_URL}/{key}",
                params={
                    "format": "jsondata",
                    "lastNObservations": self.last_n,
                },
                timeout=self.settings.http_timeout_s,
                headers={
                    "Accept": "application/vnd.sdmx.data+json;version=1.0.0-wd",
                    "User-Agent": "desk/0.1",
                },
            )
        return out

    def parse(self, raw: dict[str, Any]) -> list[Observation]:
        obs: list[Observation] = []
        for ours, payload in raw.items():
            for period, value in parse_sdmx(payload):
                obs.append(
                    Observation(
                        series=ours,
                        date=period_to_date(period),
                        value=value,
                        source=SOURCE,
                        meta={"period": period},
                    )
                )
        return obs
