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

Key conventions, so that views line up across reports: use the region names 'USA', 'Euro area', 'Switzerland', 'UK', 'Japan', 'China', 'Emerging Markets'; GICS sector names ('Information Technology', 'Industrials', 'Materials', 'Communication Services', 'Utilities', 'Healthcare', 'Banks', 'Insurance', 'Real Estate', 'Consumer Staples', 'Consumer Discretionary', 'Energy'); targets as '<Index> <Mon-YY>' (e.g. 'S&P 500 Dec-26'); rates as '<Rate> <Mon-YY>' (e.g. 'Fed funds Dec-26', 'ECB deposit Dec-26', 'US 10y Dec-26'); FX as 'EUR-USD Dec-26'; commodities as 'Gold Dec-26', 'Brent 6-12m'; stocks by their ticker.
