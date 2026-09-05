"""AAII investor sentiment survey (weekly).

The public page shows only the latest week ("This week's results ... Week ending <date> Bullish x% Neutral y%
Bearish z%"), so one observation per fetch. The crowd factor needs a multi-year range, so AAII is treated as
unavailable until 52 weekly observations exist (desk.crowd.AAII_MIN_WEEKS) unless the member-only historical
spreadsheet is imported with `desk aaii-backfill <sentiment.xls|.xlsx|.csv>` (see backfill()).

Raw payload: {"html": "..."}."""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any

from desk.sources.base import Fetcher, Observation, utcnow

SOURCE = "aaii"
SERIES = "AAII_BULL_BEAR_SPREAD"
URL = "https://www.aaii.com/sentimentsurvey"
_WEEK = re.compile(r"Week\s+ending\s+([A-Z][a-z]+ \d{1,2},? \d{4})", re.I)
_PCT = re.compile(r"(Bullish|Bearish|Neutral)\s*(\d{1,2}(?:\.\d)?)\s*%", re.I)


def _strip(html: str) -> str:
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text)


def parse_page(html: str) -> tuple[dt.date, dict[str, float]] | None:
    """(week-ending date, {"bullish": x, "neutral": y, "bearish": z}) from the results block, else None."""
    text = _strip(html)
    m = _WEEK.search(text)
    if not m:
        return None
    for fmt in ("%B %d, %Y", "%B %d %Y"):
        try:
            week = dt.datetime.strptime(m.group(1), fmt).date()
            break
        except ValueError:
            continue
    else:
        return None
    block = text[m.end() : m.end() + 400]
    found: dict[str, float] = {}
    for k, v in _PCT.findall(block):
        found.setdefault(
            k.lower(), float(v)
        )  # first hit after the date is this week's, not the average
    if "bullish" not in found or "bearish" not in found:
        return None
    return week, found


class AaiiFetcher(Fetcher):
    name = SOURCE
    attempts = 2

    def _raw(self) -> dict[str, str]:
        import httpx

        with httpx.Client(
            timeout=self.settings.http_timeout_s,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 desk/0.1"},
        ) as client:
            r = client.get(URL)
            r.raise_for_status()
            return {"html": r.text}

    def parse(self, raw: dict[str, Any]) -> list[Observation]:
        parsed = parse_page(raw.get("html") or "")
        if parsed is None:
            return []
        week, found = parsed
        return [
            Observation(
                series=SERIES,
                date=week,
                value=round(found["bullish"] - found["bearish"], 2),
                source=SOURCE,
                meta={
                    "bullish": found["bullish"],
                    "bearish": found["bearish"],
                    "neutral": found.get("neutral"),
                },
            )
        ]


# ---------------------------------------------------------------------------------------------- backfill
def _rows_from_file(path: Path) -> list[dict[str, Any]]:
    import pandas as pd

    if path.suffix.lower() == ".csv":
        df = pd.read_csv(path)
    else:
        # the AAII spreadsheet has a few title rows above the header; find the row that holds "Date"
        head = pd.read_excel(path, header=None, nrows=15)
        header_row = next(
            (
                i
                for i, row in head.iterrows()
                if any(str(c).strip().lower().startswith("date") for c in row.tolist())
            ),
            0,
        )
        df = pd.read_excel(path, header=header_row)
    df.columns = [str(c).strip().lower() for c in df.columns]
    return df.to_dict("records")


def _col(row: dict[str, Any], *names: str) -> Any:
    for n in names:
        for k, v in row.items():
            if k.startswith(n):
                return v
    return None


def parse_backfill_rows(rows: list[dict[str, Any]]) -> list[Observation]:
    """Pure: rows with Date / Bullish / Neutral / Bearish (fractions or percentages) -> weekly observations."""
    import pandas as pd

    fetched = utcnow()
    obs: list[Observation] = []
    for row in rows:
        raw_d = _col(row, "date", "reported date")
        bull, bear = _col(row, "bullish"), _col(row, "bearish")
        if raw_d is None or bull is None or bear is None:
            continue
        try:
            d = pd.to_datetime(raw_d).date()
            b, s = float(bull), float(bear)
        except (TypeError, ValueError):
            continue
        if b != b or s != s:  # NaN
            continue
        if b <= 1 and s <= 1:  # fractions in the spreadsheet
            b, s = b * 100, s * 100
        neutral = _col(row, "neutral")
        try:
            n = float(neutral) if neutral is not None else None
            if n is not None and n <= 1:
                n *= 100
        except (TypeError, ValueError):
            n = None
        obs.append(
            Observation(
                series=SERIES,
                date=d,
                value=round(b - s, 2),
                source=f"{SOURCE}_backfill",
                fetched_at=fetched,
                meta={"bullish": round(b, 2), "bearish": round(s, 2), "neutral": n},
            )
        )
    return obs


def backfill(path: Path) -> list[Observation]:
    """Observations from the AAII historical spreadsheet (member download from aaii.com/sentimentsurvey/sent_results)
    or a CSV with Date, Bullish, Neutral, Bearish columns."""
    return parse_backfill_rows(_rows_from_file(Path(path)))
