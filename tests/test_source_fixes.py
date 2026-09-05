"""Recorder fixes: yfinance symbol fallback, GDELT backoff, CNN put/call in place of CBOE, AAII parse and
backfill, manual placeholders, VIX term structure and freshness in the crowd factor, foreign-currency quotes."""

from __future__ import annotations

import datetime as dt

import httpx
import pytest
from sqlmodel import select

from desk.crowd import (
    AAII_MIN_WEEKS,
    MAX_AGE_DAYS,
    crowd_long_percentile,
    range_percentile,
    vix_term_percentile,
)
from desk.db import session_scope
from desk.models import Instrument, Observation, Price
from desk.portfolio import build_portfolio
from desk.sources import build_fetchers, price_symbols
from desk.sources.aaii import AaiiFetcher, backfill, parse_backfill_rows, parse_page
from desk.sources.fear_greed import PUTCALL_SERIES, FearGreedFetcher
from desk.sources.gdelt import BACKOFF_S, REQUEST_DELAY_S, GdeltFetcher
from desk.sources.manual import ManualFetcher
from desk.sources.yfinance_source import YFinanceFetcher
from desk.universe import load_universe
from tests.conftest import load_fixture

TODAY = dt.date(2026, 9, 4)


# ------------------------------------------------------------------------------- 1. yfinance symbol fallback
def test_universe_x9i1_resolves_to_a_real_yahoo_listing():
    x9i1 = next(i for i in load_universe() if i["ticker"] == "X9I1")
    assert x9i1["isin"] == "LU1681045453"
    assert price_symbols(x9i1) == ["AUEM.PA", "AUEM.L"]
    assert x9i1["price_currency"] == "USD" and "AUEM.PA" in x9i1["price_note"]
    yf = next(f for f in build_fetchers(load_universe()) if isinstance(f, YFinanceFetcher))
    assert yf.symbols["X9I1"] == ["AUEM.PA", "AUEM.L"] and yf.symbols["VUSA"] == "VUSA.DE"


def test_yfinance_tries_candidates_in_order_and_records_the_fallback(settings, monkeypatch):
    calls: list[str] = []
    rec = [{"date": "2026-09-03", "open": 8, "high": 8, "low": 8, "close": 8.3, "volume": 1}]

    def fake_history(self, symbol):
        calls.append(symbol)
        if symbol == "AUEM.PA":
            raise RuntimeError("Quote not found")
        return rec if symbol == "AUEM.L" else []

    monkeypatch.setattr(YFinanceFetcher, "_history", fake_history)
    f = YFinanceFetcher({"X9I1": ["AUEM.PA", "AUEM.L"], "GONE": ["A.X", "B.X"]}, settings=settings)
    raw = f._raw()
    assert calls == ["AUEM.PA", "AUEM.L", "A.X", "B.X"]
    assert raw["X9I1"] == rec and raw["_symbols"] == {"X9I1": "AUEM.L"}
    assert raw["GONE"] == [] and raw["_errors"] == ["GONE (A.X: empty history; B.X: empty history)"]
    obs = f.parse(raw)
    assert [o.ticker for o in obs] == ["X9I1"]  # the bookkeeping keys never become observations
    with pytest.raises(RuntimeError):
        YFinanceFetcher({"GONE": ["A.X"]}, settings=settings)._raw()


def test_portfolio_converts_a_usd_quote_of_a_eur_holding(settings):
    from desk.fixtures import load_fixtures
    from desk.ingest.revolut import confirm_batch
    from desk.seed import load_all_seeds
    from desk.universe import sync_instruments

    load_fixtures(settings)
    with session_scope(settings) as s:
        sync_instruments(s)
        load_all_seeds(s, settings)
        confirm_batch(s, "seed:positions_2026-09-04")
        x = s.exec(select(Instrument).where(Instrument.ticker == "X9I1")).one()
        assert x.price_currency == "USD" and x.price_note
        s.add(
            Price(
                instrument_id=x.id,
                date=TODAY,
                close=8.30,
                source="test",
                fetched_at=dt.datetime(2026, 9, 4, 7),
            )
        )
        s.commit()
        view = build_portfolio(s, settings)
        row = next(p for p in view.positions if p.instrument.ticker == "X9I1")
        usd_per_eur = view.fx["USD"].per_eur
        assert usd_per_eur and row.price == pytest.approx(8.30 / usd_per_eur)
        assert row.value_native == pytest.approx(row.position.quantity * 8.30 / usd_per_eur)
        assert "USD->EUR" in row.price_source and "AUEM.PA" in row.price_note
        assert row.pnl_pct == pytest.approx((row.price / row.position.avg_cost - 1) * 100)


# ------------------------------------------------------------------------------------ 2. GDELT backoff
def _http_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://api.gdeltproject.org/api/v2/doc/doc")
    return httpx.HTTPStatusError(f"{code}", request=req, response=httpx.Response(code, request=req))


def _timeline(v: float) -> dict:
    return {"timeline": [{"series": "x", "data": [{"date": "20260903000000", "value": v}]}]}


def test_gdelt_one_429_backs_off_and_keeps_going(settings, monkeypatch):
    sleeps: list[float] = []
    attempts = {"n": 0}

    def fake_get(url, *, params=None, timeout=None, headers=None):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise _http_error(429)
        return _timeline(1.0 if params["mode"] == "timelinetone" else 40.0)

    monkeypatch.setattr("desk.sources.gdelt.http_get_json", fake_get)
    f = GdeltFetcher(["ECB", "oil price"], settings=settings, sleep=sleeps.append)
    raw = f._raw()
    assert set(raw) == {"ECB", "oil price"} and "_errors" not in raw
    assert sleeps[0] == BACKOFF_S[0]  # the 429 waited before the retry
    assert sleeps.count(REQUEST_DELAY_S) == 3  # a pause before each of the other three requests
    assert attempts["n"] == 5
    obs = f.parse(raw)
    assert {o.topic for o in obs} == {"ECB", "oil price"} and all(
        o.meta["volume"] == 40.0 for o in obs
    )


def test_gdelt_a_dead_query_is_recorded_not_fatal(settings, monkeypatch):
    def fake_get(url, *, params=None, timeout=None, headers=None):
        if params["query"] == "ECB":
            raise _http_error(429)  # never recovers
        if params["query"] == "SpaceX":
            raise _http_error(404)  # not retryable
        return _timeline(0.5)

    sleeps: list[float] = []
    monkeypatch.setattr("desk.sources.gdelt.http_get_json", fake_get)
    f = GdeltFetcher(["ECB", "oil price", "SpaceX"], settings=settings, sleep=sleeps.append)
    raw = f._raw()
    assert "oil price" in raw and "ECB" not in raw and "SpaceX" not in raw
    assert len(raw["_errors"]) == 2 and raw["_errors"][0].startswith("ECB: 429")
    assert [s for s in sleeps if s in BACKOFF_S] == list(BACKOFF_S)  # 2, 4, 8 then give up
    assert [o.topic for o in f.parse(raw)] == ["oil price"]
    monkeypatch.setattr(
        "desk.sources.gdelt.http_get_json", lambda *a, **k: (_ for _ in ()).throw(_http_error(429))
    )
    with pytest.raises(RuntimeError, match="every query failed"):
        GdeltFetcher(["ECB"], settings=settings, sleep=sleeps.append)._raw()


# ------------------------------------------------------------------- 3. put/call from CNN instead of CBOE
def test_fear_greed_payload_yields_cnn_put_call(settings):
    obs = FearGreedFetcher(settings=settings).parse(load_fixture("fear_greed.json"))
    pc = [o for o in obs if o.series == PUTCALL_SERIES]
    assert len(pc) > 200 and all(0.3 < o.value < 1.5 for o in pc)
    assert max(o.date for o in pc) >= dt.date(2026, 9, 3)
    assert min(o.date for o in pc) <= dt.date(2025, 9, 15)  # a year of daily points
    assert "5-day average" in pc[0].meta["note"]
    assert any(o.series == "CNN_FEAR_GREED" for o in obs)


def test_cboe_source_is_gone():
    with pytest.raises(ModuleNotFoundError):
        import desk.sources.cboe  # noqa: F401
    assert not any(f.name == "cboe" for f in build_fetchers(load_universe()))


# ----------------------------------------------------------------------------------- 4. AAII parse + backfill
def test_aaii_page_parses_this_weeks_block_not_the_faq(settings):
    html = load_fixture("aaii.json")["html"]
    week, found = parse_page(html)
    assert week == dt.date(2026, 9, 2)
    assert found == {"bullish": 39.7, "neutral": 22.7, "bearish": 37.6}
    obs = AaiiFetcher(settings=settings).parse({"html": html})
    assert len(obs) == 1 and obs[0].value == pytest.approx(2.1) and obs[0].date == week
    assert obs[0].meta["bullish"] == 39.7 and obs[0].meta["bearish"] == 37.6
    assert AaiiFetcher(settings=settings).parse({"html": "<p>nothing here</p>"}) == []


def test_aaii_backfill_rows_and_csv(tmp_path):
    rows = [
        {"date": "2024-01-04", "bullish": 0.487, "neutral": 0.271, "bearish": 0.242},
        {"date": "2024-01-11", "bullish": 48.6, "neutral": 26.3, "bearish": 25.1},
        {"date": None, "bullish": 1, "bearish": 1},  # spreadsheet footer
        {"date": "2024-01-18", "bullish": "n/a", "bearish": 0.2},
    ]
    obs = parse_backfill_rows(rows)
    assert [o.date for o in obs] == [dt.date(2024, 1, 4), dt.date(2024, 1, 11)]
    assert obs[0].value == pytest.approx(24.5) and obs[0].meta["bullish"] == 48.7
    assert obs[1].value == pytest.approx(23.5) and obs[1].meta["neutral"] == 26.3
    assert all(o.series == "AAII_BULL_BEAR_SPREAD" and o.source == "aaii_backfill" for o in obs)
    p = tmp_path / "sentiment.csv"
    p.write_text(
        "Date,Bullish,Neutral,Bearish\n2025-06-05,0.33,0.30,0.37\n2025-06-12,0.36,0.31,0.33\n"
    )
    got = backfill(p)
    assert [round(o.value, 1) for o in got] == [-4.0, 3.0]


def test_aaii_unavailable_until_52_weeks(settings):
    with session_scope(settings) as s:
        for i in range(AAII_MIN_WEEKS - 1):
            s.add(
                Observation(
                    series="AAII_BULL_BEAR_SPREAD",
                    date=TODAY - dt.timedelta(weeks=i),
                    value=float(i % 10),
                    source="test",
                    fetched_at=dt.datetime(2026, 9, 4),
                )
            )
        s.commit()
        p, info = range_percentile(s, "AAII_BULL_BEAR_SPREAD", today=TODAY)
        assert p is None and "need at least 52" in info["note"]
        s.add(
            Observation(
                series="AAII_BULL_BEAR_SPREAD",
                date=TODAY - dt.timedelta(weeks=AAII_MIN_WEEKS - 1),
                value=3.0,
                source="test",
                fetched_at=dt.datetime(2026, 9, 4),
            )
        )
        s.commit()
        p, info = range_percentile(s, "AAII_BULL_BEAR_SPREAD", today=TODAY)
        assert p is not None and info["n"] == 52 and "not 3 years" in info["note"]


def test_manual_placeholders_are_skipped(settings):
    raw = {
        "observations": [
            {"series": "EGX30", "value": 0, "as_of": "2026-08-15", "note": "PLACEHOLDER — update"},
            {
                "series": "EGX30",
                "value": 31250.5,
                "as_of": "2026-09-03",
                "note": "egx.com.eg close",
            },
        ]
    }
    obs = ManualFetcher(settings=settings).parse(raw)
    assert [(o.series, o.value) for o in obs] == [("EGX30", 31250.5)]
    assert (
        ManualFetcher(settings=settings).parse(load_fixture("manual.json")) == []
    )  # all placeholders


# ------------------------------------------------------------------------ crowd: VIX term structure, freshness
def _px(s, ticker: str, d: dt.date, close: float) -> None:
    inst = s.exec(select(Instrument).where(Instrument.ticker == ticker)).one()
    s.add(
        Price(
            instrument_id=inst.id,
            date=d,
            close=close,
            source="test",
            fetched_at=dt.datetime(2026, 9, 4),
        )
    )


def test_vix_term_structure_percentile_and_freshness(settings):
    from desk.universe import sync_instruments

    with session_scope(settings) as s:
        sync_instruments(s)
        for i in range(30):
            d = TODAY - dt.timedelta(days=i)
            _px(s, "^VIX", d, 14.0 + i * 0.5)  # rising into the past: today is the low
            _px(s, "^VIX3M", d, 20.0)
        s.commit()
        p, info = vix_term_percentile(s, today=TODAY)
        assert p == pytest.approx(0.0) and info["vix"] == 14.0 and info["vix3m"] == 20.0
        assert (
            info["derived_from"] == ["prices:^VIX", "prices:^VIX3M"]
            and "not 3 years" in info["note"]
        )
        # composite orientation: a low ratio (complacency) is crowd long -> 100 - p
        vusa = s.exec(select(Instrument).where(Instrument.ticker == "VUSA")).one()
        comp, inputs = crowd_long_percentile(s, vusa, TODAY)
        assert inputs["composite_of"] == ["VIX_TERM_RATIO"] and comp == pytest.approx(100.0)
        assert "need at least 52" in inputs["AAII_BULL_BEAR_SPREAD"]["note"]
        # freshness: the same series read 60 days later is excluded
        later = TODAY + dt.timedelta(days=MAX_AGE_DAYS + 15)
        p2, info2 = vix_term_percentile(s, today=later)
        assert p2 is None and "days old" in info2["note"]


def test_real_fixture_vix3m_history_is_too_old_and_says_so(settings):
    """The recorded ^VIX3M history stops on 2026-07-17, so the term-structure signal is excluded with a note
    rather than silently used."""
    from desk.fixtures import load_fixtures

    load_fixtures(settings)
    with session_scope(settings) as s:
        p, info = vix_term_percentile(s, today=TODAY)
        assert p is None and info["as_of"] == "2026-07-17" and "days old" in info["note"]
        pc, pinfo = range_percentile(s, PUTCALL_SERIES, today=TODAY)
        assert pc is not None and pinfo["n"] > 200


def test_fred_window_covers_thirteen_monthly_prints(settings):
    """The recorded CPI series (400-day window) has 12 prints: one short of a y/y. The fetcher now asks
    for at least 480 days regardless of the price lookback."""
    from desk.sources.fred import MIN_LOOKBACK_DAYS, FredFetcher

    cpi = load_fixture("fred.json")["CPIAUCSL"]["observations"]
    assert len(cpi) == 12  # what the Mac recorded; explains 'Inflation unknown' until re-recorded
    f = FredFetcher(settings=settings)
    assert (dt.date.today() - f.start).days >= MIN_LOOKBACK_DAYS >= 400


# ------------------------------------------------------------------------------ ECB: ICP -> HICP dataset
def _sdmx(periods: list[str], values: list[float]) -> dict:
    return {
        "dataSets": [
            {
                "series": {
                    "0:0:0:0:0:0": {"observations": {str(i): [v] for i, v in enumerate(values)}}
                }
            }
        ],
        "structure": {"dimensions": {"observation": [{"values": [{"id": p} for p in periods]}]}},
    }


def test_ecb_prefers_the_hicp_dataset_and_falls_back_to_the_retired_icp_key(settings, monkeypatch):
    from desk.sources.ecb import SERIES, EcbFetcher

    assert SERIES["EZ_HICP"][0] == "HICP/M.U2.N.000000.4D0.ANR"
    assert SERIES["EZ_HICP_CORE"][0] == "HICP/M.U2.N.XEF000.4D0.ANR"
    today = dt.date(2026, 9, 5)
    calls: list[str] = []

    def fake_get(self, key):
        calls.append(key)
        if key.startswith("HICP/M.U2.N.000000"):
            return _sdmx(["2026-06", "2026-07", "2026-08"], [3.2, 3.4, 4.0])
        if key.startswith("HICP/"):
            raise RuntimeError("404")  # pretend the core key is not there yet
        if key.startswith("ICP/"):
            return _sdmx(["2025-11", "2025-12"], [2.1, 1.9])  # discontinued: stale
        return _sdmx(["2026-06-17"], [2.25])

    monkeypatch.setattr(EcbFetcher, "_get", fake_get)
    f = EcbFetcher(settings=settings, today=today)
    raw = f._raw()
    assert raw["_keys"]["EZ_HICP"] == "HICP/M.U2.N.000000.4D0.ANR"
    assert (
        raw["_keys"]["EZ_HICP_CORE"] == "ICP/M.U2.N.XEF000.4.ANR"
    )  # stale fallback kept, and logged
    assert any("EZ_HICP_CORE HICP/" in e for e in raw["_errors"])
    assert calls.count("ICP/M.U2.N.000000.4.ANR") == 0  # the fresh HICP payload stopped the search
    obs = f.parse(raw)
    by = {}
    for o in obs:
        by.setdefault(o.series, []).append(o)
    assert by["EZ_HICP"][-1].date == dt.date(2026, 8, 1) and by["EZ_HICP"][-1].value == 4.0
    assert by["EZ_HICP_CORE"][-1].date == dt.date(2025, 12, 1)
    assert "_keys" not in by and by["ECB_DEPO"][-1].value == 2.25


def test_ecb_recorded_fixture_is_the_retired_icp_series(settings):
    """What the Mac recorded on 2026-09-05: the ICP keys, last print 2025-12. The regime classifier excludes
    it (see test_regime_classifier); re-recording with the HICP keys is what fixes it."""
    from desk.sources.ecb import EcbFetcher, parse_sdmx

    raw = load_fixture("ecb.json")
    assert parse_sdmx(raw["EZ_HICP"])[-1][0] == "2025-12"
    assert (
        EcbFetcher(settings=settings, today=dt.date(2026, 9, 5))._current(raw["EZ_HICP"]) is False
    )
    assert (
        EcbFetcher(settings=settings, today=dt.date(2026, 9, 5))._current(raw["ECB_DEPO"]) is True
    )


# ------------------------------------------------------------ Alpha Vantage: pacing, throttle retry, partial
def test_alphavantage_paces_calls_and_keeps_partial_results(settings, tmp_path, monkeypatch):
    from desk.sources.alphavantage import (
        CALL_SPACING_S,
        THROTTLE_RETRY_S,
        AlphaVantageFetcher,
        CallBudget,
    )

    monkeypatch.setattr(settings, "alphavantage_api_key", "k")
    sleeps: list[float] = []
    calls: list[str] = []
    note = {
        "Note": "Please consider spreading out your free API requests more sparingly (1 request per second)"
    }

    def fake_get(url, params=None, **kw):
        key = params.get("tickers") or params.get("topics")
        calls.append(key)
        if key == "NVDA" and calls.count("NVDA") == 1:
            return note  # first NVDA call is throttled, the retry succeeds
        if key == "economy_macro":
            return note  # throttled twice -> skipped
        return {"feed": []}

    monkeypatch.setattr("desk.sources.alphavantage.http_get_json", fake_get)
    f = AlphaVantageFetcher(
        ["TSLA", "NVDA"],
        topics=["economy_macro", "financial_markets"],
        settings=settings,
        budget=CallBudget(tmp_path / "b.json", limit=25),
        sleep=sleeps.append,
    )
    raw = f._raw()
    assert set(raw["tickers"]) == {"TSLA", "NVDA"} and set(raw["topics"]) == {"financial_markets"}
    assert raw["_errors"] == [f"economy_macro: alphavantage throttled: {note['Note']}"]
    assert calls == ["TSLA", "NVDA", "NVDA", "economy_macro", "economy_macro", "financial_markets"]
    assert sleeps.count(THROTTLE_RETRY_S) == 2 and sleeps.count(CALL_SPACING_S) == 5
    assert f.attempts == 1  # the batch is never re-run wholesale
    monkeypatch.setattr(
        "desk.sources.alphavantage.http_get_json", lambda url, params=None, **kw: note
    )
    with pytest.raises(RuntimeError, match="nothing fetched"):
        AlphaVantageFetcher(
            ["TSLA"],
            topics=[],
            settings=settings,
            budget=CallBudget(tmp_path / "c.json", limit=25),
            sleep=sleeps.append,
        )._raw()


def test_wikipedia_tables_are_fetched_with_a_user_agent(monkeypatch):
    from desk import screener

    seen = {}

    class R:
        text = "<table><tr><th>Symbol</th><th>Security</th><th>GICS Sector</th></tr><tr><td>BRK.B</td><td>Berkshire</td><td>Financials</td></tr></table>"

        def raise_for_status(self):
            pass

    def fake_get(url, headers=None, **kw):
        seen["url"], seen["ua"] = url, headers["User-Agent"]
        return R()

    monkeypatch.setattr("httpx.get", fake_get)
    rows = screener.fetch_wikipedia_constituents("sp500")
    assert seen["url"] == screener.WIKI["sp500"] and "desk" in seen["ua"]
    assert rows == [
        {
            "ticker": "BRK-B",
            "name": "Berkshire",
            "sector": "Financials",
            "exchange": "NYSE/NASDAQ",
            "region": "USA",
            "currency": "USD",
            "source_symbol": "BRK-B",
        }
    ]
