"""
Universe loader — the top-of-funnel filter that the Researcher loops through.

How the universe.csv gets created (manual, weekly):
1. Go to Screener.in (premium subscription) and run your fundamental screen.
   Example filters: ROCE > 20%, Debt-to-Equity < 0.5, Sales Growth (3yr) > 15%,
   Market Cap > 500 Cr (to avoid micro-cap manipulation risk).
2. Export the results as CSV (premium feature, up to 50 custom columns).
3. Save the file as universe/universe.csv, overwriting the previous week's file.
4. Commit the file or just drop it in this directory — the pipeline picks it up
   automatically at the start of each Researcher cycle.

Why this matters:
- The Researcher (Claude Sonnet 5) is now analyzing 50-100 pre-vetted, high-quality
  names, not trawling 5,000 stocks from scratch each cycle.
- Kite Connect + web search handle all live data from this point forward — the
  Screener screen is a one-time, human-curated quality filter, not a runtime
  dependency or a scraping target.
- The CSV can also include your own annotations (e.g., "WATCHING - wait for breakout"
  or "AVOID - promoter pledging high") which the Researcher can read as context.
"""

import csv
import os
from dataclasses import dataclass


UNIVERSE_CSV_PATH = os.path.join(os.path.dirname(__file__), "universe.csv")


@dataclass
class UniverseEntry:
    ticker: str          # Trading symbol (NSE or BSE), e.g. "DIXON"
    company_name: str
    sector: str
    market_cap_cr: float
    exchange: str        # "NSE" or "BSE" — which exchange to trade on
    notes: str           # optional human annotation


def load_universe() -> list[UniverseEntry]:
    """
    Reads universe.csv and returns a list of tickers for this week's scan.
    Raises FileNotFoundError with a clear message if the file hasn't been
    exported from Screener yet.
    """
    if not os.path.exists(UNIVERSE_CSV_PATH):
        raise FileNotFoundError(
            "universe/universe.csv not found. "
            "Export your Screener.in screen as CSV and save it there before running."
        )

    entries = []
    with open(UNIVERSE_CSV_PATH, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            entries.append(UniverseEntry(
                ticker=row.get("Ticker", row.get("Symbol", "")).strip().upper(),
                company_name=row.get("Company", row.get("Name", "")).strip(),
                sector=row.get("Sector", "").strip(),
                market_cap_cr=float(row.get("Market Cap", 0) or 0),
                exchange=row.get("Exchange", "NSE").strip().upper() or "NSE",
                notes=row.get("Notes", "").strip(),
            ))

    if not entries:
        raise ValueError("universe.csv is empty — check your Screener export.")

    return entries


def get_tickers() -> list[str]:
    """Convenience wrapper — returns just the ticker symbols."""
    return [e.ticker for e in load_universe() if e.ticker]
