"""ECB Data Portal (SDMX-JSON). Raw payload: {"<OUR_SERIES_NAME>": <sdmx json>}

Series mapping (ours -> ECB keys, tried in order; the first whose latest period is recent wins):
    ECB_DEPO        FM.B.U2.EUR.4F.KR.DFR.LEV        deposit facility rate, business daily
    EZ_HICP        HICP.M.U2.N.000000.4D0.ANR       headline HICP, y/y % (HICP dataset, DSD ECB_ICP3)
    EZ_HICP_CORE   HICP.M.U2.N.XEF000.4D0.ANR       HICP ex energy & food, y/y %

The ICP dataset was discontinued on 4 February 2026 (its last print is 2025-12) and replaced by HICP after
Eurostat's methodological change; the old ICP keys remain as fallbacks. `_keys` in the payload records which
key served each series.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from desk.sources.base import Fetcher, Observation, http_get_json

log = logging.getLogger(__name__)

SOURCE = "ecb"
BASE_URL = "https://data-api.ecb.europa.eu/service/data"
SERIES: dict[str, list[str]] = {
    "ECB_DEPO": ["FM/B.U2.EUR.4F.KR.DFR.LEV"],
    "EZ_HICP": ["HICP/M.U2.N.000000.4D0.ANR", "ICP/M.U2.N.000000.4.ANR"],
    "EZ_HICP_CORE": ["HICP/M.U2.N.XEF000.4D0.ANR", "ICP/M.U2.N.XEF000.4.ANR"],
}
MAX_AGE_DAYS = (
    90  # a monthly series whose last period is older than this is treated as discontinued
)


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
        self,
        series: dict[str, str | list[str]] | None = None,
        settings=None,
        last_n: int = 36,
        today: date | None = None,
    ) -> None:
        super().__init__(settings)
        self.series = series or SERIES
        self.last_n = last_n
        self.today = today or date.today()

    def _get(self, key: str) -> Any:
        return http_get_json(
            f"{BASE_URL}/{key}",
            params={"format": "jsondata", "lastNObservations": self.last_n},
            timeout=self.settings.http_timeout_s,
            headers={
                "Accept": "application/vnd.sdmx.data+json;version=1.0.0-wd",
                "User-Agent": "desk/0.1",
            },
        )

    def _current(self, payload: Any) -> bool:
        pairs = parse_sdmx(payload) if isinstance(payload, dict) else []
        if not pairs:
            return False
        return period_to_date(pairs[-1][0]) >= self.today - timedelta(days=MAX_AGE_DAYS)

    def _raw(self) -> dict[str, Any]:
        """Try each key in order; keep the first payload whose latest period is recent. If none is recent,
        keep the last payload that returned data (and log it) so a stale series is still visible on the tape."""
        out: dict[str, Any] = {}
        used: dict[str, str] = {}
        errors: list[str] = []
        for ours, spec in self.series.items():
            keys = [spec] if isinstance(spec, str) else list(spec)
            stale: tuple[str, Any] | None = None
            for key in keys:
                try:
                    payload = self._get(key)
                except Exception as exc:  # noqa: BLE001 - a retired key answers 404; try the next one
                    errors.append(f"{ours} {key}: {exc}")
                    continue
                if self._current(payload):
                    out[ours], used[ours] = payload, key
                    break
                if parse_sdmx(payload):
                    stale = (key, payload)
                    errors.append(f"{ours} {key}: latest period is not recent")
            if ours not in out and stale is not None:
                log.warning("ecb %s: every key is stale; keeping %s", ours, stale[0])
                out[ours], used[ours] = stale[1], stale[0]
        if not out:
            raise RuntimeError("ecb: no series returned data: " + "; ".join(errors))
        out["_keys"] = used
        if errors:
            out["_errors"] = errors
        return out

    def parse(self, raw: dict[str, Any]) -> list[Observation]:
        obs: list[Observation] = []
        for ours, payload in raw.items():
            if ours.startswith("_") or not isinstance(payload, dict):
                continue
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
