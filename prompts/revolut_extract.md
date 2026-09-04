You are reading a screenshot of the Revolut app showing a user's investment holdings. Return ONLY valid JSON, no prose:

{
  "as_of": "YYYY-MM-DD or null if no date is visible",
  "positions": [
    {"ticker": "TSLA", "name": "Tesla", "pot": "brokerage|commodities|robo",
     "quantity": 10.26, "last_price": 367.16, "currency": "USD", "value": 3769.26, "return_pct": -8.19,
     "note": "anything unclear"}
  ],
  "totals": {"brokerage": 29884.52, "commodities": 49676.18, "robo": 620.85, "total": 80181.55},
  "notes": ["anything hidden behind 'Show more', collapsed pots, or numbers you could not read"]
}

Rules:
- One entry per visible holding. Copy numbers exactly as shown; do not compute what you cannot see. Use null for anything not visible.
- Cash lines use ticker 'CASH_<CCY>' with quantity = the cash amount and last_price = 1.
- A pot whose composition is collapsed (e.g. the commodities pot) is one entry with ticker 'COMMODITIES_POT' (or 'ROBO_ADVISOR'), quantity null and value = the pot total.
- If a pot header total exceeds the sum of visible lines, add an entry with ticker 'UNKNOWN_<POT>' whose value is the difference and say so in note.
- Currency is the currency the price is displayed in (EUR for Xetra/Euronext ETF lines, USD for US stocks).
- return_pct is the percentage return Revolut shows for the line, negative for losses.
