"""SEC Form 4 (insider transactions) from EDGAR, docs/BRIEF.md 8d.

Pipeline per run: the company_tickers.json map (ticker -> CIK, cached a week), then the daily form index for
each of the last `days` business days plus today, filtered to Form 4 filings by issuers in our universe, then
the complete submission .txt of each filing, from which the ownershipDocument XML is cut out and parsed.

EDGAR rules: a User-Agent that names you and an e-mail address, and no more than 10 requests a second.
DESK_EDGAR_USER_AGENT carries the header; without an "@" in it the fetcher reports itself disabled.

Raw payload (JSON-serialisable, recorded as tests/fixtures/form4.json):
    {"as_of": "YYYY-MM-DD", "days": [...index dates...], "tickers": {"NVDA": "1045810", ...},
     "filings": [{"cik": "...", "ticker": "NVDA", "adsh": "0001...", "filed": "YYYY-MM-DD",
                  "url": "https://www.sec.gov/Archives/edgar/data/.../....txt", "xml": "<ownershipDocument>..."}],
     "_errors": [...]}
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from typing import Any

from desk.sources.base import Fetcher, Observation, utcnow

log = logging.getLogger(__name__)

SOURCE = "form4"
COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
DAILY_INDEX_URL = "https://www.sec.gov/Archives/edgar/daily-index/{year}/QTR{q}/form.{ymd}.idx"
ARCHIVES_URL = "https://www.sec.gov/Archives/"
REQUEST_SPACING_S = 0.12  # under EDGAR's 10 requests/second
OPEN_MARKET_CODES = {"P": "buy", "S": "sell"}
CODE_NAMES = {
    "P": "open-market purchase",
    "S": "open-market sale",
    "A": "grant/award",
    "M": "option exercise",
    "F": "tax withholding",
    "G": "gift",
    "J": "other",
    "C": "conversion",
    "D": "disposition to issuer",
    "X": "option exercise",
    "W": "will/inheritance",
}


def business_days(end: dt.date, n: int) -> list[dt.date]:
    """The last n business days ending at `end` (inclusive), oldest first. Holidays show up as 404s."""
    out: list[dt.date] = []
    d = end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= dt.timedelta(days=1)
    return sorted(out)


def parse_form_index(text: str) -> list[dict[str, str]]:
    """Rows of a daily form.YYYYMMDD.idx: form type, company, CIK, date filed, file name."""
    rows: list[dict[str, str]] = []
    started = False
    for line in text.splitlines():
        if not started:
            if line.startswith("---"):
                started = True
            continue
        parts = line.split()
        if len(parts) < 5:
            continue
        file_name, filed, cik = parts[-1], parts[-2], parts[-3]
        form = parts[0]
        company = " ".join(parts[1:-3])
        if not (cik.isdigit() and filed.isdigit() and file_name.startswith("edgar/")):
            continue
        rows.append(
            {"form": form, "company": company, "cik": cik, "filed": filed, "file": file_name}
        )
    return rows


_XML_BLOCK = re.compile(r"<XML>\s*(.*?)\s*</XML>", re.S | re.I)


def extract_ownership_xml(submission_txt: str) -> str | None:
    """The ownershipDocument XML embedded in a complete submission .txt."""
    for m in _XML_BLOCK.finditer(submission_txt):
        block = m.group(1)
        if "ownershipDocument" in block:
            return block.strip()
    if "<ownershipDocument" in submission_txt:
        start = submission_txt.index("<ownershipDocument")
        end = submission_txt.find("</ownershipDocument>", start)
        if end > 0:
            return submission_txt[start : end + len("</ownershipDocument>")]
    return None


def _text(el: ET.Element | None, path: str) -> str | None:
    if el is None:
        return None
    node = el.find(path)
    if node is None:
        return None
    v = (node.text or "").strip()
    if not v and node.find("value") is not None:
        v = (node.find("value").text or "").strip()
    return v or None


def _float(v: str | None) -> float | None:
    if v in (None, ""):
        return None
    try:
        return float(v.replace(",", ""))
    except ValueError:
        return None


def _bool(v: str | None) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes")


def parse_ownership_xml(xml_text: str) -> dict[str, Any]:
    """Issuer, reporting owners, transactions and footnotes from one ownershipDocument. Pure."""
    root = ET.fromstring(xml_text)
    issuer = root.find("issuer")
    footnotes = {
        f.get("id"): (f.text or "").strip()
        for f in root.findall("footnotes/footnote")
        if f.get("id")
    }
    owners = []
    for o in root.findall("reportingOwner"):
        rel = o.find("reportingOwnerRelationship")
        title = _text(rel, "officerTitle")
        roles = []
        if _bool(_text(rel, "isDirector")):
            roles.append("director")
        if _bool(_text(rel, "isOfficer")):
            roles.append(title or "officer")
        if _bool(_text(rel, "isTenPercentOwner")):
            roles.append("10% owner")
        if _bool(_text(rel, "isOther")):
            roles.append(_text(rel, "otherText") or "other")
        owners.append(
            {
                "name": _text(o, "reportingOwnerId/rptOwnerName") or "unknown",
                "cik": _text(o, "reportingOwnerId/rptOwnerCik"),
                "role": ", ".join(roles) or None,
                "is_officer_or_director": bool(
                    _bool(_text(rel, "isDirector")) or _bool(_text(rel, "isOfficer"))
                ),
            }
        )
    doc_10b5 = _bool(_text(root, "aff10b5One"))
    txns = []
    for table, asset_type in (("nonDerivativeTable", "stock"), ("derivativeTable", "option")):
        t = root.find(table)
        if t is None:
            continue
        tag = "nonDerivativeTransaction" if asset_type == "stock" else "derivativeTransaction"
        for tx in t.findall(tag):
            coding = tx.find("transactionCoding")
            amounts = tx.find("transactionAmounts")
            code = _text(coding, "transactionCode")
            fn_ids = [f.get("id") for f in tx.iter("footnoteId")]
            notes = " ".join(footnotes.get(i, "") for i in fn_ids)
            txns.append(
                {
                    "asset_type": asset_type,
                    "security": _text(tx, "securityTitle"),
                    "date": _text(tx, "transactionDate"),
                    "code": code,
                    "shares": _float(_text(amounts, "transactionShares")),
                    "price": _float(_text(amounts, "transactionPricePerShare")),
                    "acquired_disposed": _text(amounts, "transactionAcquiredDisposedCode"),
                    "is_10b5_1": doc_10b5
                    or _bool(_text(coding, "aff10b5One"))
                    or "10b5-1" in notes.lower(),
                    "footnotes": notes[:300],
                }
            )
    return {
        "issuer_ticker": (_text(issuer, "issuerTradingSymbol") or "").upper(),
        "issuer_name": _text(issuer, "issuerName"),
        "issuer_cik": _text(issuer, "issuerCik"),
        "period": _text(root, "periodOfReport"),
        "owners": owners,
        "transactions": txns,
    }


def classify(tx: dict[str, Any]) -> dict[str, Any]:
    """Brief 8d classification: only codes P and S are open-market; everything else is asset_type=other, score 0."""
    code = (tx.get("code") or "").upper()
    if code in OPEN_MARKET_CODES and tx.get("asset_type") == "stock":
        return {"side": OPEN_MARKET_CODES[code], "asset_type": "stock", "is_open_market": True}
    side = "buy" if (tx.get("acquired_disposed") or "").upper() == "A" else "sell"
    return {"side": side, "asset_type": "other", "is_open_market": False}


def trades_from_raw(
    raw: dict[str, Any], fetched_at: dt.datetime | None = None
) -> list[dict[str, Any]]:
    """Flat trade rows (one per owner x transaction) from a recorded payload. Pure."""
    fetched = fetched_at or utcnow()
    rows: list[dict[str, Any]] = []
    for filing in raw.get("filings") or []:
        try:
            doc = parse_ownership_xml(filing["xml"])
        except ET.ParseError as exc:
            log.warning("form4 %s: unparseable XML: %s", filing.get("url"), exc)
            continue
        ticker = doc["issuer_ticker"] or (filing.get("ticker") or "").upper()
        filed = dt.date.fromisoformat(filing["filed"])
        owners = doc["owners"] or [
            {"name": "unknown", "role": None, "is_officer_or_director": False}
        ]
        # a joint filing (an entity and the person behind it) is one economic filer: the first owner
        # carries the row, the others are named in the role, and the transaction is counted once
        lead = next((o for o in owners if o["is_officer_or_director"]), owners[0])
        others = [o["name"] for o in owners if o is not lead]
        role = lead["role"] or ""
        if others:
            role = (role + " " if role else "") + f"(joint with {', '.join(others)})"
        for tx in doc["transactions"]:
            if not tx.get("date"):
                continue
            traded = dt.date.fromisoformat(tx["date"][:10])
            cls = classify(tx)
            rows.append(
                {
                    "source": SOURCE,
                    "filer_name": lead["name"],
                    "filer_role": role or None,
                    "is_officer_or_director": any(o["is_officer_or_director"] for o in owners),
                    "co_filers": others,
                    "issuer_ticker": ticker,
                    "trade_date": traded,
                    "filed_date": filed,
                    "lag_days": (filed - traded).days,
                    "transaction_code": tx.get("code"),
                    "code_name": CODE_NAMES.get((tx.get("code") or "").upper(), tx.get("code")),
                    "quantity": tx.get("shares"),
                    "price": tx.get("price"),
                    "security": tx.get("security"),
                    "is_10b5_1": bool(tx.get("is_10b5_1")),
                    "footnotes": tx.get("footnotes"),
                    "raw_url": filing["url"],
                    "fetched_at": fetched,
                    **cls,
                }
            )
    return rows


class Form4Fetcher(Fetcher):
    name = SOURCE
    attempts = 1  # per-request handling below; the daily index is small and the filings are many

    def __init__(
        self,
        tickers: list[str],
        settings=None,
        days: int = 2,
        today: dt.date | None = None,
        sleep=time.sleep,
    ) -> None:
        super().__init__(settings)
        self.tickers = [t.upper() for t in tickers]
        self.days = days
        self.today = today or dt.date.today()
        self._sleep = sleep
        self._last = 0.0

    @property
    def user_agent(self) -> str | None:
        return getattr(self.settings, "edgar_user_agent", None)

    def enabled(self) -> tuple[bool, str | None]:
        ua = self.user_agent or ""
        if "@" not in ua:
            return False, "DESK_EDGAR_USER_AGENT must name you and an e-mail address (EDGAR policy)"
        if not self.tickers:
            return False, "no tickers to watch"
        return True, None

    def _get(self, url: str) -> str:
        import httpx

        wait = REQUEST_SPACING_S - (time.monotonic() - self._last)
        if wait > 0:
            self._sleep(wait)
        self._last = time.monotonic()
        r = httpx.get(
            url,
            headers={"User-Agent": self.user_agent, "Accept-Encoding": "gzip, deflate"},
            timeout=self.settings.http_timeout_s,
            follow_redirects=True,
        )
        r.raise_for_status()
        return r.text

    def cik_map(self) -> dict[str, str]:
        """ticker -> CIK from company_tickers.json, cached for a week under data/cache."""
        path = self.settings.cache_dir / "edgar_company_tickers.json"
        try:
            if path.exists() and (utcnow().timestamp() - path.stat().st_mtime) < 7 * 86400:
                doc = json.loads(path.read_text(encoding="utf-8"))
            else:
                raise FileNotFoundError
        except (OSError, ValueError):
            doc = json.loads(self._get(COMPANY_TICKERS_URL))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(doc), encoding="utf-8")
        out: dict[str, str] = {}
        for entry in doc.values() if isinstance(doc, dict) else doc:
            t = str(entry.get("ticker", "")).upper()
            if t:
                out[t] = str(entry.get("cik_str"))
        return out

    def _raw(self) -> dict[str, Any]:
        errors: list[str] = []
        ciks = self.cik_map()
        wanted = {}
        for t in self.tickers:
            cik = ciks.get(t) or ciks.get(t.replace("-", "."))
            if cik:
                wanted[cik] = t
        filings: list[dict[str, Any]] = []
        days = business_days(self.today, self.days + 1)
        for d in days:
            url = DAILY_INDEX_URL.format(
                year=d.year, q=(d.month - 1) // 3 + 1, ymd=d.strftime("%Y%m%d")
            )
            try:
                text = self._get(url)
            except Exception as exc:  # noqa: BLE001 - a holiday or not-yet-published day is a 404
                errors.append(f"index {d}: {exc}")
                continue
            for row in parse_form_index(text):
                if row["form"] != "4" or row["cik"] not in wanted:
                    continue
                f_url = ARCHIVES_URL + row["file"]
                try:
                    xml = extract_ownership_xml(self._get(f_url))
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{row['file']}: {exc}")
                    continue
                if xml is None:
                    errors.append(f"{row['file']}: no ownershipDocument")
                    continue
                filings.append(
                    {
                        "cik": row["cik"],
                        "ticker": wanted[row["cik"]],
                        "adsh": row["file"].rsplit("/", 1)[-1].replace(".txt", ""),
                        "filed": dt.datetime.strptime(row["filed"], "%Y%m%d").date().isoformat(),
                        "url": f_url,
                        "xml": xml,
                    }
                )
        out: dict[str, Any] = {
            "as_of": self.today.isoformat(),
            "days": [d.isoformat() for d in days],
            "tickers": {t: c for c, t in wanted.items()},
            "filings": filings,
        }
        if errors:
            out["_errors"] = errors
        return out

    def parse(self, raw: dict[str, Any]) -> list[Observation]:
        """Form 4 rows are not time series; they are stored by desk.flow.store_trades. Nothing here."""
        return []

    def trades(self, raw: dict[str, Any]) -> list[dict[str, Any]]:
        return trades_from_raw(raw)
