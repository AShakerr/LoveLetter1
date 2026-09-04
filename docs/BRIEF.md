# Investment Desk — build brief

This is the spec for `desk`. Sections are numbered as in the original brief; the kickoff prompt (section 0) is omitted.

## 1. What this is

A daily-refreshed dashboard that decides what to buy, hold, trim and sell across a small Revolut portfolio, driven by three inputs: live market data, daily news sentiment, and Bank J. Safra Sarasin research PDFs the user receives weekly or biweekly. It does not trade. It produces a ranked decision list with reasoning, logs every decision with its inputs, and scores itself over time so the user can see whether the system's calls are any good.

The design principle: **a decision is an argument with its weights shown.** The user should be able to open any decision and see exactly which inputs produced it, which rule fired, and what would reverse it.

## 2. Universe and constraints

- Instruments the user can actually buy on Revolut: US-listed stocks, UCITS ETFs (EUR and USD share classes), Revolut commodity pots, crypto via Revolut. The system must maintain a `tradable` flag per instrument and never recommend something without it.
- Base currency for reporting: EUR. Store native currency per instrument, convert at day's close for portfolio views. The user's salary is EUR, so FX exposure of the whole book is a first-class metric.
- Position limits (configurable in `config/limits.yaml`, these are the defaults):
  - Max single position: 15% of total portfolio
  - Max single theme (e.g. "gold", "AI capex", "energy"): 35%
  - Max illiquid / private (SPCX and similar): 15%
  - Min diversified core (broad index ETFs): 40% target, warn below 25%
  - Max crypto: 5%

## 3. Data sources

All free tier. Each gets a fetcher in `desk/sources/` with a `fetch() -> list[Observation]` interface, retry, and a cached last-good value so a dead API never blanks the dashboard.

| Source | What | How |
|---|---|---|
| yfinance | Daily OHLCV for every instrument in the universe, plus ^GSPC, ^NDX, ^GDAXI, ^STOXX50E, ^VIX, GC=F, CL=F, BZ=F, HG=F, SI=F, BTC-USD, ETH-USD, EURUSD=X, EURGBP=X | Daily at 07:00 Europe/Berlin |
| FRED | DFF (Fed funds), DGS2, DGS10, DGS30, CPIAUCSL, CPILFESL, T10Y2Y, UNRATE | API key in env; daily |
| ECB Data Portal | Deposit facility rate, HICP headline and core (series `ICP.M.U2.N.000000.4.ANR` and `ICP.M.U2.N.XEF000.4.ANR`) | REST, daily |
| Alpha Vantage NEWS_SENTIMENT | Ticker-level news sentiment scores for held and watchlist tickers plus topics `economy_macro`, `energy_transportation`, `financial_markets` | Free tier is 25 calls/day; budget them, cache aggressively |
| GDELT DOC 2.0 | Global news tone for a fixed query set: "Strait of Hormuz", "Federal Reserve rate", "ECB", "oil price", "Egypt IMF", "SpaceX" | No key; daily, store tone and article volume |
| CNN Fear & Greed | Composite score | Unofficial JSON endpoint; treat as optional, degrade gracefully |
| CFTC COT | Net speculative positioning in gold, crude, copper | Weekly (Friday); optional in phase 3 |
| Safra PDFs | Dropped into `inbox/` by the user | Claude API extraction, section 5 |
| Revolut screenshots | Dropped into `inbox/portfolio/` | Claude API vision extraction into `positions` table; user confirms in UI before it becomes live |

Egypt: no reliable free API for EGX30 or CBE. Keep a manual `manual_observations.yaml` the user can edit; the dashboard shows the as-of date in red if older than 14 days.

## 4. Data model (SQLModel, SQLite)

```
instruments(id, ticker, name, kind[stock|etf|commodity|crypto|private], currency, exchange, tradable, theme, sector, region)
prices(instrument_id, date, open, high, low, close, volume)            unique(instrument_id, date)
observations(id, series, date, value, source, fetched_at)              e.g. series="DGS10"
news_sentiment(id, instrument_id|null, topic|null, date, score, volume, source)
positions(id, instrument_id, quantity, avg_cost, currency, pot[brokerage|commodities|robo], as_of, confirmed_by_user)
house_views(id, report_id, scope[region|sector|asset|index_target|rate|fx|commodity], key, stance[most_preferred|neutral|least_preferred|overweight|underweight], value, changed_from, quote, page)
reports(id, publisher, kind, date, filename, sha256, extracted_at, raw_json)
scores(id, instrument_id, date, total, f_safra, f_regime, f_portfolio, f_valuation, f_momentum, f_season, inputs_json)
rules_fired(id, position_id, date, rule, severity[mandatory|review], detail_json)
decisions(id, date, instrument_id, action[BUY|ADD|HOLD|TRIM|SELL|AVOID], size_pct, score_id, rules_json, reasoning_md, created_at, user_status[pending|executed|skipped|overridden], user_note, executed_at)
regime(id, date, label, inflation_state, policy_state, oil_state, vol_state, inputs_json)
```

`decisions` is append-only. Never update a decision's content; the user's response goes in `user_status` and `user_note`. This is what makes the system scoreable later.

## 5. Safra PDF extraction (Claude API)

`desk/ingest/safra.py`. On a new file in `inbox/`:

1. Hash it; skip if `reports.sha256` exists.
2. Send the PDF to `claude-sonnet-4-5` (or whatever is current) with the document in the message and the extraction prompt below. Ask for JSON only. Validate against a Pydantic schema; on failure, retry once with the validation error appended; on second failure, store `raw_json = null` and flag in the UI.
3. Write rows to `house_views`. For each view, compare to the most recent prior view with the same `scope` + `key` and populate `changed_from` so upgrades and downgrades are first-class events.
4. Move the file to `archive/reports/YYYY-MM-DD_<kind>.pdf`.

Extraction prompt (store in `prompts/safra_extract.md`):

```
You are extracting the house view from a Bank J. Safra Sarasin research report. Return ONLY valid JSON matching this schema, no prose:

{
  "publisher": "Safra Sarasin",
  "kind": "cross_asset_weekly | economic_outlook | equity_focus_list | market_views | other",
  "date": "YYYY-MM-DD",
  "headline_stance": {"equities": "...", "bonds": "...", "credit": "...", "duration_preference": "..."},
  "views": [
    {"scope":"region|sector|asset|index_target|rate|fx|commodity|stock",
     "key":"e.g. 'Euro area', 'Industrials', 'S&P 500 Dec-26', 'Fed funds Dec-26', 'EUR-USD Dec-26', 'Brent 6-12m', 'NVDA'",
     "stance":"most_preferred|neutral|least_preferred|overweight|underweight|buy|hold|sell|null",
     "value": "number or range as string, or null",
     "changed_from": "prior stance if the report says it changed, else null",
     "quote": "the sentence that supports this, verbatim, max 200 chars",
     "page": integer}
  ],
  "risks": [{"risk":"...", "quote":"...", "page":int}],
  "market_performance_table": [{"name":"...", "level":..., "pe":..., "tr_1w":..., "tr_ytd":...}],
  "forecast_tables": {"rates":[...], "yields":[...], "fx":[...], "gdp_cpi":[...], "index_targets":[...]}
}

Rules: quote exact figures, never round. If a table is graphical and unreadable, omit it rather than guess. Flag internal inconsistencies in a top-level "inconsistencies" array.
```

The user has six reports from August 2026 already extracted in `docs/seed/house_views_2026-08.json`. Load these as the initial state in phase 2 so the dashboard is not empty.

## 6. Regime classifier

`desk/regime.py`, runs daily, writes one `regime` row. Deliberately simple and legible; four dimensions, each a label with the inputs that produced it:

- `inflation_state`: headline vs core gap. `energy_shock` if headline − core > 0.75pp; `broad` if core > 3%; `contained` otherwise.
- `policy_state`: from FRED DFF trend and the last three house-view rate forecasts. `hiking`, `on_hold`, `cutting`.
- `oil_state`: Brent 20-day change and level. `shock` if > $90 and > +15% over 60 days; `elevated`, `normal`.
- `vol_state`: VIX level. `complacent` < 15, `normal`, `stressed` > 25.

The `label` is a human sentence assembled from these, e.g. "Energy-shock inflation, central banks hiking, oil shock, equity vol complacent." This label goes at the top of every decision's reasoning so the user always knows what world the decision was made in.

## 7. Conviction score

`desk/score.py`. Six factors, each 0 to 5, weights fixed:

| Factor | Weight | Computed from |
|---|---|---|
| Safra alignment | 25 | Latest `house_views` for the instrument's sector, region and asset class. most_preferred = 5, neutral = 2.5, least_preferred = 0. Direct stock rating overrides: buy 5 / hold 2.5 / sell 0. Recent upgrade adds +0.5, downgrade −0.5. |
| Regime fit | 20 | A static table in `config/regime_fit.yaml` mapping (theme, regime dimension) to a score. E.g. energy under `oil_state=shock` scores 4 on momentum but the fit table asks "does it survive the reverse?" so energy scores 2. This table is opinionated and the user edits it. |
| Portfolio fit | 15 | Marginal diversification: 5 if adding this reduces the largest theme weight, 1 if it increases a theme already above 35%, penalise correlation to the largest holding using 90-day return correlation from `prices`. |
| Valuation | 15 | For indices/ETFs: current P/E vs 5-year median from stored observations; for house-covered instruments: distance to Safra target. 5 = cheap, 0 = expensive. |
| Momentum | 15 | 3-month price return percentile within the universe plus news sentiment 14-day average. Equal weight. |
| Seasonality | 10 | Only two rules earn points, per the evidence review in `docs/seasonality_evidence.md`: +3 if the date is in Nov–Dec and the instrument is a broad equity index or cyclical sector; +2 more if cyclical (industrials, materials, discretionary, tech). Everything else scores 0. Do not add more rules without adding evidence to that doc. |

Total = Σ(factor/5 × weight), range 0–100. Bands: 75+ act, 60–74 candidate, 45–59 watch, <45 avoid. Store every factor and its inputs JSON so the UI can render the breakdown.

## 8. Rules engine and decisions

`desk/rules.py`. Runs after scoring.

**Mandatory rules** (severity `mandatory`, produce SELL/TRIM decisions the user must respond to):
- `stop_loss`: close < entry × (1 − stop), stop default 18% for stocks, 12% for ETFs, none for private. Configurable per position.
- `max_position`: position > 15% of total → TRIM to 15%.
- `max_theme`: theme > 35% → TRIM the largest position in that theme down to bring theme to 35%.
- `thesis_invalidated`: a position's stored `kill_condition` (free text + a machine-checkable predicate where possible, e.g. `house_view(sector).stance == least_preferred` or `observation('BZ=F') < 80`) evaluates true.
- `house_downgrade_to_least`: Safra moves the position's sector or region to least_preferred → SELL flag.

**Review rules** (severity `review`, produce a flag, not an action):
- `score_decay`: score fell > 15 points over 30 days or below 45.
- `momentum_break`: 3-month return turned negative while sentiment fell below −0.15.
- `stale_data`: any input older than 7 days (14 for manual).
- `concentration_warning`: diversified core < 25%.

**Buy side**: any tradable instrument with score ≥ 75 and no rule conflicts becomes a BUY candidate; ADD if already held under its limit. Size = min(cash available, limit headroom, 5% of portfolio per decision). Never propose more BUYs than there is cash plus pending TRIM/SELL proceeds.

Every decision writes `reasoning_md`: regime label, score breakdown, which rules fired, the kill condition being attached, and one line on what would reverse it. Generated by a template first; in phase 4, optionally passed through Claude for a readable paragraph, but the template version is always stored too.

## 9. Phases and acceptance

**Phase 1 — skeleton and data.** Repo, Docker Compose, SQLite, all fetchers with tests using recorded fixtures, daily scheduler, a bare dashboard that shows the tape (S&P, Brent, gold, 10y, VIX, Fed funds, EZ HICP, BTC) from real data. Accept when: `docker compose up` shows today's numbers and `pytest` passes with the network off.

**Phase 2 — house views and portfolio.** Safra extraction pipeline, seed load of August reports, Revolut screenshot ingestion with user confirmation, portfolio view with theme weights, FX exposure and the limit bars. Accept when: dropping a PDF in `inbox/` produces `house_views` rows with `changed_from` populated and the UI shows the upgrade/downgrade log.

**Phase 3 — regime, scores, rules, decisions.** Everything in sections 6 to 8. Decision list with pending/executed/skipped/overridden. Accept when: the daily job produces a decision list, each decision opens to a full breakdown, and marking one executed updates positions.

**Phase 4 — self-scoring and polish.** For every decision older than 30/90 days, compute what happened vs. the alternative (held vs. sold, bought vs. skipped) and show hit rate and P&L attribution by rule and by factor. Weekly email digest. Optional Claude-written reasoning. Accept when: the "Track record" page shows at least the seeded August ideas scored against actual prices.

## 10. Deploy

- `docker-compose.yml`: one `app` service (FastAPI + scheduler in one process, uvicorn), one volume for `data/` (SQLite, archive, inbox). No database server.
- `.env`: `ANTHROPIC_API_KEY`, `FRED_API_KEY`, `ALPHAVANTAGE_API_KEY`, `DESK_BASIC_AUTH_USER/PASS`, `TZ=Europe/Berlin`.
- Basic auth on every route. Caddy in front for TLS on the Hetzner box, same pattern as the IMPROVR dashboard.
- Daily job 07:00 Berlin. Manual "Run now" button in the UI.
- Backups: nightly `sqlite3 .backup` to `data/backups/`, keep 30, plus rsync to the existing IMPROVR backup target.

## 11. Seed content (already researched, include in `docs/seed/`)

- `house_views_2026-08.json`: Safra positioning from the 12–28 August reports. Equities slight OW, bonds slight UW, credit neutral, 5–7y duration. Most preferred regions: USA, Euro area (upgraded, prefer unhedged). Switzerland downgraded to neutral. Most preferred sectors: IT, Industrials (upgraded), Materials, Comm Services, Utilities. Healthcare downgraded to neutral. Energy least preferred. Targets Dec-26: S&P 8,200; Euro Stoxx 50 6,900; DAX 28,000; MSCI EM 1,750; gold $4,600; Brent $75–80; Fed funds 4.25%; ECB 2.50%; US 10y 4.50%. Named risks: Q2 EPS growth of 34.2% was the peak; hyperscaler depreciation 8%→22% of sales by 2030; 3 of 5 hyperscalers FCF-negative; private credit defaults rising; very strong El Niño.
- `positions_2026-09-04.json`: Commodities pot $49,676 (composition unknown, user to confirm); SPCX 75.73 @ $150.43; TSLA 10.26 @ $367.16; X9I1 306.48 @ €7.75; 4COP 25.6 @ €61.26; VUSA 9.76 @ €126.47; cash $4,240.79; Robo $620.85; $4,475 brokerage unidentified.
- `seasonality_evidence.md`: the verdict table. What holds: Q4 best quarter (all in Nov–Dec), December 74% hit rate, global Nov–Apr effect (t = 9.7), cyclicals lead Nov–Apr. What fails: September effect, Santa rally as forecast, midterm Q4 (n = 19), January effect, gold Q4, Bitcoin Q4. Counter-literature: Sullivan/Timmermann/White 2001, Harvey/Liu/Zhu 2016, McLean & Pontiff 2016.
- `regime_2026-09-04.json`: energy-shock inflation (US 3.4 vs 2.5 core, EZ 3.3 with energy +14.3), policy hiking (ECB hiked June, Fed 9–3 hold with 3 hike dissents), oil shock (Brent $95.8, Hormuz closed since Feb), vol complacent (VIX 14.3).

## 12. What this deliberately does not do

- It does not auto-trade. Revolut has no API and a system that "decides for you" is only trustworthy if every decision is logged before you act on it.
- It does not backtest the scoring model on history. The Safra input does not exist historically in structured form, so a backtest would be fiction. The track record is built forward from the first real decision.
- It does not add seasonality rules, alternative data or ML without a written evidence note in `docs/`. The model's legibility is the point.
- It is not investment advice. It is a decision journal with an opinionated scoring engine, and its author is not a financial adviser.
