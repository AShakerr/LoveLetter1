"""Each parser against its recorded fixture. Pure functions, no network."""

from datetime import date

from desk.sources.alphavantage import AlphaVantageFetcher
from desk.sources.ecb import EcbFetcher, parse_sdmx, period_to_date
from desk.sources.fear_greed import FearGreedFetcher
from desk.sources.fred import FredFetcher
from desk.sources.gdelt import GdeltFetcher
from desk.sources.manual import ManualFetcher
from desk.sources.yfinance_source import YFinanceFetcher, frame_to_records
from tests.conftest import load_fixture


def test_yfinance_parse_prices(settings):
    raw = load_fixture("yfinance.json")
    obs = YFinanceFetcher({}, settings=settings).parse(raw)
    tickers = {o.ticker for o in obs}
    assert {"^GSPC", "BZ=F", "GC=F", "^VIX", "BTC-USD", "TSLA"} <= tickers
    assert all(o.is_price and o.source == "yfinance" for o in obs)
    last_brent = max((o for o in obs if o.ticker == "BZ=F"), key=lambda o: o.date)
    rec = max(raw["BZ=F"], key=lambda r: r["date"])
    assert last_brent.value == rec["close"] and last_brent.date == date.fromisoformat(rec["date"])
    assert last_brent.meta["high"] == rec["high"]


def test_yfinance_frame_to_records_skips_nan():
    import pandas as pd

    df = pd.DataFrame(
        {
            "Open": [1.0, 2.0],
            "High": [1.5, 2.5],
            "Low": [0.5, 1.5],
            "Close": [1.2, float("nan")],
            "Volume": [10, 20],
        },
        index=pd.to_datetime(["2026-09-01", "2026-09-02"]),
    )
    recs = frame_to_records(df)
    assert len(recs) == 1 and recs[0]["date"] == "2026-09-01" and recs[0]["close"] == 1.2


def test_fred_parse_skips_missing_values(settings):
    raw = load_fixture("fred.json")
    obs = FredFetcher(settings=settings).parse(raw)
    series = {o.series for o in obs}
    assert {"DFF", "DGS10", "DGS2", "CPIAUCSL"} <= series
    assert all(o.source == "fred" for o in obs)
    dots = [r for r in raw["DGS10"]["observations"] if r["value"] == "."]
    assert dots, "fixture should contain a '.' holiday row to prove it is skipped"
    assert not any(
        o.series == "DGS10" and o.date == date.fromisoformat(dots[0]["date"]) for o in obs
    )


def test_ecb_sdmx_parse(settings):
    raw = load_fixture("ecb.json")
    pairs = parse_sdmx(raw["EZ_HICP"])
    assert pairs and all(isinstance(v, float) for _, v in pairs)
    obs = EcbFetcher(settings=settings).parse(raw)
    assert {o.series for o in obs} == {"ECB_DEPO", "EZ_HICP", "EZ_HICP_CORE"}
    hicp = max((o for o in obs if o.series == "EZ_HICP"), key=lambda o: o.date)
    assert hicp.date.day == 1 and hicp.meta["period"].count("-") == 1
    assert period_to_date("2026-07") == date(2026, 7, 1)
    assert period_to_date("2026-09-02") == date(2026, 9, 2)


def test_alphavantage_parse_aggregates_per_day(settings):
    raw = load_fixture("alphavantage.json")
    obs = AlphaVantageFetcher([], settings=settings).parse(raw)
    tsla = [o for o in obs if o.ticker == "TSLA"]
    assert tsla and all(o.is_news for o in tsla)
    feed = raw["tickers"]["TSLA"]["feed"]
    day = feed[0]["time_published"][:8]
    d = date(int(day[:4]), int(day[4:6]), int(day[6:8]))
    scores = [
        float(ts["ticker_sentiment_score"])
        for it in feed
        if it["time_published"][:8] == day
        for ts in it["ticker_sentiment"]
        if ts["ticker"] == "TSLA"
    ]
    got = next(o for o in tsla if o.date == d)
    assert abs(got.value - sum(scores) / len(scores)) < 1e-9 and got.meta["volume"] == len(scores)
    topics = {o.topic for o in obs if o.topic}
    assert "economy_macro" in topics


def test_gdelt_parse_joins_tone_and_volume(settings):
    raw = load_fixture("gdelt.json")
    obs = GdeltFetcher(settings=settings).parse(raw)
    horm = [o for o in obs if o.topic == "Strait of Hormuz"]
    assert horm and all(o.meta["volume"] is not None for o in horm)
    assert {o.topic for o in obs} == set(raw.keys())


def test_fear_greed_parse(settings):
    raw = load_fixture("fear_greed.json")
    obs = FearGreedFetcher(settings=settings).parse(raw)
    assert obs and all(o.series == "CNN_FEAR_GREED" for o in obs)
    latest = max(obs, key=lambda o: o.date)
    assert latest.value == raw["fear_and_greed"]["score"]
    assert latest.meta["rating"] == raw["fear_and_greed"]["rating"]


def test_manual_parse(settings):
    raw = load_fixture("manual.json")
    obs = ManualFetcher(settings=settings).parse(raw)
    assert {o.series for o in obs} == {"EGX30", "CBE_DEPOSIT_RATE"}
    assert all(o.source == "manual" for o in obs)
