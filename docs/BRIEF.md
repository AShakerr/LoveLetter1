# Investment Desk — build brief

This is the spec for `desk`. Sections are numbered as in the original brief; the kickoff prompt (section 0) is omitted.

## 1. What this is

A daily-refreshed dashboard that decides what to buy, hold, trim and sell across a small Revolut portfolio, driven by three inputs: live market data, daily news sentiment, and Bank J. Safra Sarasin research PDFs the user receives weekly or biweekly. It starts as a decision journal with a paper-trading shadow book, and becomes a trading bot only once its own track record passes the promotion criteria in section 8b. It produces a ranked decision list with reasoning, logs every decision with its inputs, fills them on paper with realistic costs, and scores itself over time so the user can see whether the system's calls are any good before real money follows them.

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

## 7b. Crowd factor (positioning and surprise)

Sentiment level says how people feel. What moves prices is the gap between outcome and expectation, and how much of the expected outcome is already bought. This factor measures those two things and nothing else.

**Weights change.** Momentum drops from 15 to 10 (pure price trend: 3-month return percentile within the universe, nothing else). Seasonality drops from 10 to 5. The freed 10 points become **Crowd**. Total stays 100. Updated table:

| Factor | Weight |
|---|---|
| Safra alignment | 25 |
| Regime fit | 20 |
| Portfolio fit | 15 |
| Valuation | 15 (redefined in 7c) |
| Momentum | 10 |
| Crowd | 10 |
| Seasonality | 5 |

**Inputs, all free:**

| Signal | Source | Cadence | What it measures |
|---|---|---|---|
| CFTC Commitments of Traders, net speculative position as percentile of 3-year range | cftc.gov CSV, Legacy report, futures-only | Weekly (Fri) | Crowding in gold, crude, copper, S&P e-mini, EUR, 10y notes |
| Put/call ratio (CBOE total) and VIX term structure (VIX vs VIX3M) | CBOE daily CSV, yfinance ^VIX ^VIX3M | Daily | Hedging demand; backwardation = stress |
| AAII bull-bear spread | aaii.com weekly survey | Weekly (Thu) | Retail stated positioning |
| Macro surprise | Store consensus before and actual after for: payrolls, CPI, core CPI, PCE, ISM, EZ HICP, FOMC and ECB decisions vs market-implied odds the day before | Per event | Whether the outcome beat what was priced |
| Earnings surprise | Beat rate vs 10-year average, and price reaction on the day vs the beat (a beat that sells off is a crowded name) | Per season | Whether good news is already owned |
| Consensus gap on the house view | Safra index targets vs sell-side consensus (yfinance analyst targets aggregated for index constituents, or the median from a fixed set of published strategist targets in `manual_observations.yaml`) | Monthly | Whether the bank is the crowd |

**Scoring, 0 to 5, contrarian at extremes and confirming in the middle:**

For an instrument, take the positioning signal most relevant to its theme (COT for commodities and FX, put/call and AAII for equities). Express it as a percentile P of its 3-year range.

- P above 90 or below 10: crowded. Score **1** for a position in the crowd's direction, **4** against it. The trade everyone already has is the one with nobody left to buy.
- P between 30 and 70: no information from positioning. Score **3**, then adjust ±1 for surprise: last relevant macro or earnings surprise in the instrument's favour adds 1, against subtracts 1.
- P between 10 and 30 or 70 and 90: mildly stretched. Score **2** with the crowd, **3** against.
- Consensus gap: if the Safra target for the instrument's index is within 2% of consensus, cap the Safra alignment factor's contribution at 4/5 instead of 5/5 and note "house view is consensus" in the reasoning. If Safra is more than 5% away from consensus in the direction of the trade, no change (the view carries information whether or not it turns out right).

**Deferral rule (rules engine):** a BUY or ADD decision whose instrument has a scheduled event (FOMC, ECB, CPI, payrolls, its own earnings) within the next 2 trading days, AND whose positioning percentile is above 80 or below 20, is created with status `deferred` and a reason, and re-evaluated the day after the event. Mandatory exits are never deferred.

**What this does not claim.** None of this predicts what the crowd will do. It measures what the crowd has already done and what it already expects, which is the only observable part. Say that in the reasoning template.

## 7c. Valuation factor, redefined (P/E, forward P/E, PEG)

The 15-point Valuation factor becomes explicit and instrument-aware.

**Data.** yfinance `Ticker.info` gives `trailingPE`, `forwardPE`, `pegRatio`, `priceToBook`, `enterpriseToEbitda`, `freeCashflow`, `totalDebt`, `ebitda`, `revenueGrowth`, `earningsGrowth`, `targetMeanPrice`, `numberOfAnalystOpinions`, `recommendationMean`. It is free, occasionally stale or missing, and PEG in particular depends on analyst growth estimates that are frequently wrong. Refresh fundamentals **weekly** (Sunday job) for the whole universe, not daily; they do not change daily and the calls are slow. Alpha Vantage `OVERVIEW` is the fallback for a name yfinance returns nothing for, within the 25-call daily budget. Store every field in a new `fundamentals(instrument_id, date, field, value, source)` table so the UI can show what the score was built on and when.

For ETFs and indices, use the index P/E from the Safra market performance table (already in `house_views`) and the fund's own reported P/E where yfinance has it; PEG does not apply, so the factor uses P/E vs 5-year median only.

**Scoring for a single stock, 0 to 5, three components averaged:**

1. *PEG.* Below 1.0 → 5. 1.0 to 1.5 → 4. 1.5 to 2.0 → 3. 2.0 to 3.0 → 2. Above 3.0 → 1. Negative or missing earnings → 0 and a "no earnings" flag; the name cannot score above 60 total.
2. *Forward P/E vs sector.* Compute the sector median forward P/E across the universe each week. Z-score the name against it. Below −1 → 5, −1 to −0.3 → 4, −0.3 to +0.3 → 3, +0.3 to +1 → 2, above +1 → 1.
3. *Trailing P/E vs own history.* Compared with the name's 5-year median trailing P/E from stored weekly fundamentals (build up over time; until 52 weeks exist, use the sector component twice). Same banding as 2.

**Value-trap gate.** A low multiple on collapsing earnings is not cheap. If `earningsGrowth` or `revenueGrowth` is negative, or trailing P/E is below 8 while forward P/E is higher than trailing (earnings expected to fall), cap the Valuation factor at 2 and add the flag "possible value trap". Energy at 13x with earnings −10.9% y/y is the live example.

**Quality gate (applies to the screener in 8c, not to held positions).** A name enters the candidate pool only if: free cash flow positive, net debt / EBITDA below 3 (financials exempt), revenue growth positive, at least 5 analyst opinions. These are not scores, they are the floor.

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

## 8b. Execution layer: broker interface, paper fills, live guards

The system starts as a decision journal and earns the right to execute. The interface is fixed from phase 3 so that switching from paper to live changes one config line and zero decision logic.

### Interface (`desk/broker/base.py`)

```python
class Order(BaseModel):
    decision_id: int
    instrument_id: int
    side: Literal["BUY", "SELL"]
    quantity: Decimal | None      # exactly one of quantity / notional
    notional: Decimal | None      # in instrument currency
    order_type: Literal["MARKET", "LIMIT"] = "MARKET"
    limit_price: Decimal | None = None
    time_in_force: Literal["DAY", "GTC"] = "DAY"
    client_ref: str               # f"desk-{decision_id}-{date}" so retries are idempotent

class Fill(BaseModel):
    order_id: str
    filled_at: datetime
    quantity: Decimal
    price: Decimal
    fees: Decimal
    currency: str
    slippage_bps: Decimal | None  # vs reference price recorded on the decision

class Broker(Protocol):
    name: str
    mode: Literal["paper", "live"]
    def positions(self) -> list[Position]: ...
    def cash(self) -> dict[str, Decimal]: ...           # per currency
    def submit(self, order: Order) -> str: ...          # returns broker order id
    def cancel(self, order_id: str) -> None: ...
    def fills(self, since: datetime) -> list[Fill]: ...
    def is_tradable(self, instrument: Instrument) -> bool: ...
```

Tables to add: `orders(id, decision_id, broker, broker_order_id, client_ref, side, quantity, notional, order_type, limit_price, status[pending|submitted|filled|partial|cancelled|rejected], submitted_at, error)` and `fills(id, order_id, filled_at, quantity, price, fees, currency, slippage_bps)`. Executing an order updates `positions` through fills only, never directly.

### PaperBroker (`desk/broker/paper.py`), phase 3

Fills every MARKET order at the next session's open from `prices`, applies a fixed spread cost from `config/costs.yaml` (defaults: 5 bps ETFs, 10 bps US large caps, 25 bps everything else, 50 bps crypto) plus a flat fee if the instrument's venue charges one. LIMIT orders fill if the next session's low/high crosses the limit, otherwise expire at DAY. Records `slippage_bps` against the decision's reference close so the track record in phase 4 includes what execution would actually have cost. Paper cash and positions live in the same tables with `broker="paper"`, so the UI is identical in both modes.

The paper book is seeded from the user's real Revolut positions so the two diverge only through the system's own decisions. A "Paper vs actual" panel shows the gap: what the system would hold vs what the user actually holds after executing or skipping each decision.

### LiveBroker adapters, phase 5 only

Revolut has no API. The tradable slice moves to a broker with one. Two adapters, one built:

- `IBKRBroker`: Interactive Brokers via `ib_async`, connected to an IB Gateway container on the same Docker network. Covers US stocks, UCITS ETFs on Xetra/LSE, and FX conversion. The one to build.
- `AlpacaBroker`: US stocks and crypto only, simpler API, useful as a fallback or for a US-only sub-book. Stub.

Only the brokerage slice migrates. Commodities pot and SPCX stay on Revolut and remain manual decisions the user confirms in the UI, with `broker="manual"`.

### Live guards, all three required

1. `DESK_LIVE=1` in env, absent by default. Without it every `submit()` to a live adapter raises.
2. `config/limits.yaml: live.max_daily_notional_eur` (default 2,000) and `live.max_order_notional_eur` (default 1,000). Exceeding either rejects the order and writes a `rules_fired` row with severity mandatory so it is visible.
3. Kill switch: if `data/KILL` exists, the scheduler skips execution entirely and the UI shows a red banner. The user creates the file by hand or via one button. Removing it requires the UI button plus typing the word CONFIRM.

Additional rules for the live path:
- Mandatory-rule exits (stop_loss, max_position, max_theme, thesis_invalidated, house_downgrade_to_least) may auto-execute in live mode. Discretionary BUY and ADD decisions never auto-execute; they stay `pending` until the user approves them in the UI, then the approved order is routed through the same `submit()`.
- Never more than one order per instrument per day. Retries reuse `client_ref`.
- Live mode runs only after the market for that instrument's venue has been open 15 minutes; no opening-auction orders.
- Every live fill triggers a reconciliation: `broker.positions()` is compared to the `positions` table and any discrepancy above 0.5% of a position halts execution and flags.

### Promotion criteria, paper to live

Written down so the decision is not made on a good week. All must hold over at least 60 trading days of paper decisions:
- Mandatory rules fired at least 5 times and, in hindsight at 30 days, following them beat ignoring them on aggregate.
- Executed-equivalent paper BUY decisions, net of paper costs, are not below a 50/50 blend of VUSA and a EURO STOXX 50 ETF over the same windows.
- No data-staleness incident lasted longer than 3 days.
- The user has reviewed every decision in the log (no `pending` older than 7 days).

If the criteria fail, the system stays paper and the track-record page says why. That is a valid outcome.

## 8c. Daily screener

**What the user asked for:** every day, a list of stocks that look undervalued or likely to rise, from valuation, sentiment, news and outlooks.

**What the system will honestly do:** every day, rank a defined universe by the same six-factor conviction score, apply the quality and value-trap gates, and show the top names with their full breakdown. "Will rise" is not a claim the system makes. The claim is "scores highest on the model today, and here is why". The track record page decides whether that is worth anything.

**Universe.** `config/universe.yaml` gets a `screener` section: S&P 500 constituents, STOXX Europe 600 constituents, plus every name on the current Safra US Equity Focus List, restricted to names with `tradable: true` on Revolut. Refresh constituent lists monthly from the Wikipedia tables (free, reliable enough) and flag names that drop out. Roughly 1,100 names.

**Daily pipeline addition, after scoring:**
1. Prices for the whole screener universe (yfinance bulk download, one call).
2. Fundamentals are weekly (7c), so this step reads the stored table.
3. News sentiment: Alpha Vantage allows 25 calls a day. Spend them on the top 20 names by pre-sentiment score plus anything held. Names outside the top 20 use GDELT topic tone for their sector as a proxy and are flagged "sector-level sentiment only".
4. Score all names. Apply gates.
5. Write the top 15 and bottom 15 to a new `screener(date, instrument_id, rank, total, factors_json, gates_json)` table.

**UI.** A "Screener" page with two lists: *Candidates* (top 15, gates passed) and *Avoid* (bottom 15, or any name held that scores below 45). Each row expands to the factor breakdown, the fundamentals used with their as-of date, the sentiment source and age, and the Safra view if the name or its sector has one. A candidate with score ≥ 75 and portfolio-fit ≥ 3 gets a "Propose BUY" button that creates a normal decision with kill conditions auto-drafted from the template in 8. Nothing on the screener page is a decision until that button is pressed.

**Anti-churn.** A name has to stay in the top 15 for 3 consecutive trading days before "Propose BUY" is enabled. Sentiment is noisy day to day and a one-day spike is not a signal.

**Track record hook (phase 4).** Every day's top 15 is scored at 30/60/90 days against the S&P 500 and STOXX 600 equal-weight. This is the test of whether the screener has any edge. Show it on the track-record page from the first day, even when it is empty.

## 9. Phases and acceptance

**Phase 1 — skeleton and data.** Repo, Docker Compose, SQLite, all fetchers with tests using recorded fixtures, daily scheduler, a bare dashboard that shows the tape (S&P, Brent, gold, 10y, VIX, Fed funds, EZ HICP, BTC) from real data. Accept when: `docker compose up` shows today's numbers and `pytest` passes with the network off.

**Phase 2 — house views and portfolio.** Safra extraction pipeline, seed load of August reports, Revolut screenshot ingestion with user confirmation, portfolio view with theme weights, FX exposure and the limit bars. Accept when: dropping a PDF in `inbox/` produces `house_views` rows with `changed_from` populated and the UI shows the upgrade/downgrade log.

**Phase 3 — regime, scores, rules, decisions, paper execution.** Everything in sections 6 to 8b with PaperBroker only. Decision list with pending/executed/skipped/overridden, and a paper book that fills approved decisions at next open with costs. Accept when: the daily job produces a decision list, each decision opens to a full breakdown, approving one creates an order and a paper fill, and the "Paper vs actual" panel shows the gap.

**Phase 4 — self-scoring and polish.** For every decision older than 30/90 days, compute what happened vs. the alternative (held vs. sold, bought vs. skipped) including paper execution costs, and show hit rate and P&L attribution by rule and by factor. The promotion criteria from 8b are computed live on this page with a pass/fail per criterion. Weekly email digest. Optional Claude-written reasoning. Accept when: the "Track record" page shows at least the seeded August ideas scored against actual prices and the promotion checklist renders.

**Phase 5 — live execution, gated.** Only starts when the phase 4 promotion checklist passes and the user says so explicitly. IBKRBroker adapter, IB Gateway container, the three guards, reconciliation. Accept when: a 1-share live test order round-trips with a fill and reconciliation, the kill switch halts the scheduler, and an order over the daily cap is rejected and logged.

## Regime table additions

Add to `config/regime_fit.yaml`:

```yaml
  private_health_wearables:   # WHOOP
    inflation_state: {energy_shock: 3, broad: 2, contained: 3}
    policy_state:    {hiking: 2, on_hold: 3, cutting: 4}
    oil_state:       {shock: 3, elevated: 3, normal: 3}
    vol_state:       {complacent: 3, normal: 3, stressed: 1}
    note: "Consumer subscription hardware, pre-IPO. IPO windows close when vol is stressed and rates are rising; otherwise regime barely matters. Scored flat so the illiquid limit does the work."

  eu_telecom_income:          # ORA
    inflation_state: {energy_shock: 3, broad: 3, contained: 3}
    policy_state:    {hiking: 2, on_hold: 4, cutting: 5}
    oil_state:       {shock: 3, elevated: 3, normal: 3}
    vol_state:       {complacent: 3, normal: 4, stressed: 4}
    note: "Bond proxy with a 4.9% yield. Hiking hurts the multiple; stress helps because it is defensive. Regulated pricing means the inflation dimension is neutral."
```

## Phase 4, amended

Phase 4 now includes, in this order: (1) sections 7b and 7c, with tests that reproduce the scoring bands; (2) the weekly fundamentals job; (3) section 8c screener and its page; (4) the original phase 4 self-scoring, extended with the screener track record; (5) weekly digest. Accept when: the Screener page shows a ranked list with gates and breakdowns from real data, the deferral rule creates a `deferred` decision in a test with a mocked event calendar, and the track-record page renders the promotion checklist plus the (initially empty) screener hit-rate panel.

## 10. Deploy

- `docker-compose.yml`: one `app` service (FastAPI + scheduler in one process, uvicorn), one volume for `data/` (SQLite, archive, inbox). No database server.
- `.env`: `ANTHROPIC_API_KEY`, `FRED_API_KEY`, `ALPHAVANTAGE_API_KEY`, `DESK_BASIC_AUTH_USER/PASS`, `TZ=Europe/Berlin`, `DESK_BROKER=paper`. Phase 5 adds `DESK_LIVE`, `IB_GATEWAY_HOST/PORT`, `IB_ACCOUNT` and an `ibgateway` service in compose.
- Basic auth on every route. Caddy in front for TLS on the Hetzner box, same pattern as the IMPROVR dashboard.
- Daily job 07:00 Berlin. Manual "Run now" button in the UI.
- Backups: nightly `sqlite3 .backup` to `data/backups/`, keep 30, plus rsync to the existing IMPROVR backup target.

## 11. Seed content (already researched, include in `docs/seed/`)

- `house_views_2026-08.json`: Safra positioning from the 12–28 August reports. Equities slight OW, bonds slight UW, credit neutral, 5–7y duration. Most preferred regions: USA, Euro area (upgraded, prefer unhedged). Switzerland downgraded to neutral. Most preferred sectors: IT, Industrials (upgraded), Materials, Comm Services, Utilities. Healthcare downgraded to neutral. Energy least preferred. Targets Dec-26: S&P 8,200; Euro Stoxx 50 6,900; DAX 28,000; MSCI EM 1,750; gold $4,600; Brent $75–80; Fed funds 4.25%; ECB 2.50%; US 10y 4.50%. Named risks: Q2 EPS growth of 34.2% was the peak; hyperscaler depreciation 8%→22% of sales by 2030; 3 of 5 hyperscalers FCF-negative; private credit defaults rising; very strong El Niño.
- `positions_2026-09-04.json`: Commodities pot $49,676 (composition unknown, user to confirm); SPCX 75.73 @ $150.43; TSLA 10.26 @ $367.16; X9I1 306.48 @ €7.75; 4COP 25.6 @ €61.26; VUSA 9.76 @ €126.47; cash $4,240.79; Robo $620.85; $4,475 brokerage unidentified.
- `seasonality_evidence.md`: the verdict table. What holds: Q4 best quarter (all in Nov–Dec), December 74% hit rate, global Nov–Apr effect (t = 9.7), cyclicals lead Nov–Apr. What fails: September effect, Santa rally as forecast, midterm Q4 (n = 19), January effect, gold Q4, Bitcoin Q4. Counter-literature: Sullivan/Timmermann/White 2001, Harvey/Liu/Zhu 2016, McLean & Pontiff 2016.
- `regime_2026-09-04.json`: energy-shock inflation (US 3.4 vs 2.5 core, EZ 3.3 with energy +14.3), policy hiking (ECB hiked June, Fed 9–3 hold with 3 hike dissents), oil shock (Brent $95.8, Hormuz closed since Feb), vol complacent (VIX 14.3).

## 12. What this deliberately does not do

- It does not auto-trade until it has earned it. Phases 1 to 4 are paper only. Live execution exists behind the promotion criteria in 8b, three guards and a broker migration off Revolut, and even then only mandatory-rule exits run unattended.
- It does not backtest the scoring model on history. The Safra input does not exist historically in structured form, so a backtest would be fiction. The track record is built forward from the first real decision.
- It does not add seasonality rules, alternative data or ML without a written evidence note in `docs/`. The model's legibility is the point.
- It is not investment advice. It is a decision journal with an opinionated scoring engine, and its author is not a financial adviser.
