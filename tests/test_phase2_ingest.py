"""Safra extraction pipeline, changed_from resolution, screenshot ingestion. Claude is faked."""

import datetime as dt
import json
from pathlib import Path

from sqlmodel import select

from desk.db import session_scope
from desk.ingest.revolut import (
    backout_avg_cost,
    confirm_batch,
    discard_batch,
    ingest_screenshot,
    pending_batches,
)
from desk.ingest.safra import (
    SafraExtraction,
    View,
    extract_with_retry,
    ingest_pdf,
    resolve_changed_from,
    write_report,
)
from desk.models import HouseView, Position, Report
from desk.universe import sync_instruments

SEED = Path(__file__).parent.parent / "docs" / "seed" / "house_views_2026-08.json"


class FakeCompleter:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, system, content, *, max_tokens=16000):
        self.calls.append((system, content))
        return self.replies.pop(0)


def _report(date, kind, views):
    return {"publisher": "Safra Sarasin", "kind": kind, "date": date, "views": views}


def test_seed_reports_validate():
    doc = json.loads(SEED.read_text())
    for rep in doc["reports"]:
        ex = SafraExtraction.model_validate(rep)
        assert ex.views


def test_resolve_changed_from_rules():
    prior = HouseView(report_id=1, scope="sector", key="Energy", stance="least_preferred")
    assert (
        resolve_changed_from(View(scope="sector", key="Energy", stance="neutral"), prior)
        == "least_preferred"
    )
    # same rank, different vocabulary -> not a change
    prior_mp = HouseView(report_id=1, scope="region", key="USA", stance="most_preferred")
    assert (
        resolve_changed_from(View(scope="region", key="USA", stance="overweight"), prior_mp) is None
    )
    # explicit wins
    assert (
        resolve_changed_from(
            View(scope="region", key="USA", stance="neutral", changed_from="x"), prior_mp
        )
        == "x"
    )
    # value change on targets
    prior_v = HouseView(report_id=1, scope="index_target", key="S&P 500 Dec-26", value="8000")
    assert (
        resolve_changed_from(
            View(scope="index_target", key="S&P 500 Dec-26", value="8200"), prior_v
        )
        == "8000"
    )
    assert (
        resolve_changed_from(
            View(scope="index_target", key="S&P 500 Dec-26", value="8000"), prior_v
        )
        is None
    )
    assert resolve_changed_from(View(scope="sector", key="New", stance="neutral"), None) is None


def test_cross_report_changed_from_and_tiers(settings):
    with session_scope(settings) as s:
        r1 = SafraExtraction.model_validate(
            _report(
                "2026-08-01",
                "economic_outlook",
                [
                    {"scope": "sector", "key": "Energy", "stance": "least_preferred"},
                    {"scope": "index_target", "key": "DAX Dec-26", "value": "27000"},
                ],
            )
        )
        write_report(s, r1, filename="a.pdf", sha256="a", raw_json={})
        # tactical grid says overweight: must not be diffed against the strategic tier
        r2 = SafraExtraction.model_validate(
            _report(
                "2026-08-02",
                "market_views",
                [{"scope": "sector", "key": "Energy", "stance": "overweight"}],
            )
        )
        write_report(s, r2, filename="b.pdf", sha256="b", raw_json={})
        r3 = SafraExtraction.model_validate(
            _report(
                "2026-08-10",
                "cross_asset_weekly",
                [
                    {"scope": "sector", "key": "Energy", "stance": "neutral"},
                    {"scope": "index_target", "key": "DAX Dec-26", "value": "28000"},
                ],
            )
        )
        write_report(s, r3, filename="c.pdf", sha256="c", raw_json={})
        rows = {(v.report_id, v.key): v for v in s.exec(select(HouseView)).all()}
        assert rows[(2, "Energy")].changed_from is None
        assert rows[(3, "Energy")].changed_from == "least_preferred"
        assert rows[(3, "DAX Dec-26")].changed_from == "27000"


def test_extract_with_retry_appends_validation_error():
    good = json.dumps(
        _report(
            "2026-08-14",
            "cross_asset_weekly",
            [
                {
                    "scope": "sector",
                    "key": "Industrials",
                    "stance": "most_preferred",
                    "quote": "x",
                    "page": 12,
                }
            ],
        )
    )
    fake = FakeCompleter(
        ['```json\n{"kind": "cross_asset_weekly", "views": [{"scope": "planet"}]}\n```', good]
    )
    ex, raw, err = extract_with_retry(fake, "PROMPT", b"%PDF-1.4 fake", "x.pdf")
    assert ex is not None and err is None and ex.views[0].key == "Industrials"
    assert len(fake.calls) == 2
    retry_text = fake.calls[1][1][-1]["text"]
    assert "failed validation" in retry_text and "scope" in retry_text
    assert fake.calls[1][1][0]["type"] == "document"


def test_extract_gives_up_after_two_failures():
    fake = FakeCompleter(["not json at all", '{"kind": 1}'])
    ex, raw, err = extract_with_retry(fake, "P", b"pdf", "x.pdf")
    assert ex is None and err.startswith("attempt 2") and raw is not None


def test_ingest_pdf_end_to_end_and_dedupe(settings, tmp_path):
    good = json.dumps(
        _report(
            "2026-08-14",
            "cross_asset_weekly",
            [
                {
                    "scope": "region",
                    "key": "Euro area",
                    "stance": "most_preferred",
                    "changed_from": "neutral",
                    "quote": "Euro area equities to most preferred",
                    "page": 12,
                }
            ],
        )
    )
    pdf = settings.reports_inbox / "CrossAssetWeekly.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake report")
    with session_scope(settings) as s:
        rep = ingest_pdf(s, pdf, FakeCompleter([good]), settings)
        assert rep is not None and not rep.flagged and rep.kind == "cross_asset_weekly"
        assert not pdf.exists()
        archived = settings.reports_archive / "2026-08-14_cross_asset_weekly.pdf"
        assert archived.exists()
        hv = s.exec(select(HouseView).where(HouseView.report_id == rep.id)).one()
        assert hv.changed_from == "neutral"
        # same bytes again -> duplicate, no second report, file still cleared from the inbox
        pdf.write_bytes(b"%PDF-1.4 fake report")
        assert ingest_pdf(s, pdf, FakeCompleter([]), settings) is None
        assert not pdf.exists() and len(s.exec(select(Report)).all()) == 1


def test_ingest_pdf_flags_after_double_failure(settings):
    pdf = settings.reports_inbox / "bad.pdf"
    pdf.write_bytes(b"%PDF-1.4 bad")
    with session_scope(settings) as s:
        rep = ingest_pdf(s, pdf, FakeCompleter(["nope", "still nope"]), settings)
        assert rep.flagged and rep.raw_json is None and "attempt 2" in rep.flag_reason
        assert s.exec(select(HouseView)).first() is None
        assert list(settings.reports_archive.glob("*_other.pdf"))


def test_backout_avg_cost():
    assert abs(backout_avg_cost(150.43, -9.01) - 165.32) < 0.01
    assert backout_avg_cost(100.0, None) == 100.0
    assert backout_avg_cost(None, 5.0) is None


SHOT = json.dumps(
    {
        "as_of": "2026-09-04",
        "positions": [
            {
                "ticker": "TSLA",
                "name": "Tesla",
                "pot": "brokerage",
                "quantity": 10.26,
                "last_price": 367.16,
                "currency": "USD",
                "value": 3769.26,
                "return_pct": -8.19,
            },
            {
                "ticker": "CASH_USD",
                "pot": "brokerage",
                "quantity": 4240.79,
                "last_price": 1,
                "currency": "USD",
                "value": 4240.79,
            },
            {
                "ticker": "COMMODITIES_POT",
                "pot": "commodities",
                "quantity": None,
                "currency": "USD",
                "value": 49676.18,
                "return_pct": -15.59,
            },
            {
                "ticker": "NEWCO",
                "name": "Something new",
                "pot": "brokerage",
                "quantity": 2,
                "last_price": 10,
                "currency": "USD",
            },
        ],
        "totals": {"total": 80181.55},
        "notes": ["commodities pot collapsed"],
    }
)


def test_screenshot_ingest_confirm_discard(settings):
    img = settings.portfolio_inbox / "revolut.png"
    img.write_bytes(b"\x89PNG fake")
    with session_scope(settings) as s:
        sync_instruments(s)
        # an existing confirmed TSLA position that the new snapshot must supersede on confirm
        tsla_id = s.exec(select(Position)).first()  # none yet
        assert tsla_id is None
        from desk.models import Instrument

        tsla = s.exec(select(Instrument).where(Instrument.ticker == "TSLA")).one()
        s.add(
            Position(
                instrument_id=tsla.id,
                quantity=5,
                avg_cost=300,
                currency="USD",
                as_of=dt.date(2026, 8, 1),
                confirmed_by_user=True,
                batch="old",
                source="manual",
            )
        )
        s.commit()
        res = ingest_screenshot(s, img, FakeCompleter([SHOT]), settings)
        assert res["status"] == "pending" and res["positions"] == 4 and not img.exists()
        batch = res["batch"]
        pend = pending_batches(s)
        assert set(pend) == {batch}
        newco = s.exec(select(Instrument).where(Instrument.ticker == "NEWCO")).one()
        assert newco.kind == "other" and not newco.tradable
        pot = next(
            p
            for p in pend[batch]
            if p.instrument_id != tsla.id and p.quantity == 1.0 and p.value_native == 49676.18
        )
        assert pot.last_price == 49676.18
        n = confirm_batch(s, batch)
        assert n == 4
        old = s.exec(select(Position).where(Position.batch == "old")).one()
        assert old.closed_at is not None
        live = s.exec(
            select(Position).where(
                Position.confirmed_by_user.is_(True), Position.closed_at.is_(None)
            )
        ).all()
        assert len(live) == 4
        assert pending_batches(s) == {}
        # discard only touches pending rows
        assert discard_batch(s, batch) == 0
