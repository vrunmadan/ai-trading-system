"""
fundamentals/screener_public.py
--------------------------------
Fetches real fundamental data (ROCE, ROE, debt/equity, growth, promoter
holding trend) from Screener.in's PUBLIC company page — no login, no
premium subscription, no SCREENER_EMAIL/SCREENER_PASSWORD required.

Why this exists (2026-09-22): Varun asked how to incorporate fundamental
analysis into signal generation, and separately noted he doesn't have a
Screener premium subscription. Two things turned out to both be true:

  1. researcher/signal_generator.py's "fundamental_score" is currently pure
     news-headline sentiment (see SYSTEM_PROMPT_TEMPLATE there) — it starts
     at 60 (neutral) and is adjusted by RSS headlines only. There is no
     balance-sheet data anywhere in the pipeline today, despite the name.
  2. Screener's own docs (universe/screener_client.py) say "premium account
     required for full fundamental data and large screen exports" — true
     for run_screen() (paginating a filter across all 5000+ listed
     companies) but NOT true for a single company's own page: verified live
     (2026-09-22, via a real browser session) that screener.in/company/
     {SYMBOL}/ renders ROCE, ROE, P/E, book value, dividend yield, 3/5/10-
     year compounded sales & profit growth, the balance sheet (from which
     debt/equity is computed), and the full quarterly shareholding pattern
     (promoter holding trend) — all with NO login and NO premium tier.

So this module is what actually answers "incorporate fundamental analysis,"
and universe/screener_client.py's authenticated ScreenerClient is untouched
— it still exists for the weekly universe-DISCOVERY screen, which is a
separate concern (finding new candidate stocks across the whole market, not
scoring stocks already in the universe) and does need premium if Varun ever
runs it beyond the free tier's page/result limits.

Verified page structure (2026-09-22, live, via a real browser — not
guessed):
  ul#top-ratios > li.flex > span.name (label) + span.value > span.number
      Market Cap, Current Price, High / Low, Stock P/E, Book Value,
      Dividend Yield, ROCE, ROE, Face Value.
      Debt to equity is NOT in this list even for leveraged companies
      (checked TATASTEEL) — computed from the balance sheet instead.
  table.ranges-table (under #analysis), one per metric, first row is the
      metric's own label, rest are "{period}: | {value}%":
      Compounded Sales Growth, Compounded Profit Growth, Stock Price CAGR,
      Return on Equity.
  #balance-sheet table.data-table — "Equity Capital", "Reserves",
      "Borrowings" rows, one column per fiscal year-end. debt_to_equity =
      latest Borrowings / (latest Equity Capital + Reserves).
  #shareholding table.data-table — "Promoters", "FIIs", "DIIs",
      "Government", "Public" rows, one column per quarter. Used for the
      promoter-holding level and its trend (rising/falling), a real
      governance signal — NOT the same as promoter PLEDGE, which Screener's
      public page does not expose; that still needs an NSE shareholding-
      pattern filing if Varun wants pledge data specifically.

This structure is a live website's markup, not a versioned API — it WILL
drift eventually. Every field extraction below fails independently and
returns None for that field rather than raising, so a layout change
degrades what this can report instead of breaking the whole fetch (and,
upstream, never breaks a research cycle — see fetch_fundamentals's
docstring).
"""

import logging
import re
from typing import Optional

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

BASE_URL = "https://www.screener.in"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-IN,en;q=0.9",
}


def _clean_number(text: str) -> Optional[float]:
    """'₹ 16,78,172' / '7.78%' / '-2%' / '8%' -> float. None if not parseable."""
    if text is None:
        return None
    cleaned = re.sub(r"[₹,%\s]", "", text)
    if cleaned in ("", "-", "—"):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_top_ratios(soup: BeautifulSoup) -> dict:
    """ul#top-ratios -> {label (lowercased, spaces->underscores): float}."""
    out: dict = {}
    ul = soup.find("ul", id="top-ratios")
    if not ul:
        return out
    for li in ul.find_all("li"):
        name_el = li.find("span", class_="name")
        if not name_el:
            continue
        label = name_el.get_text(strip=True)
        number_el = li.find("span", class_="number")
        if number_el is None:
            continue
        # High / Low has TWO span.number children (high and low) — keep both.
        numbers = [ _clean_number(n.get_text()) for n in li.find_all("span", class_="number") ]
        numbers = [n for n in numbers if n is not None]
        if not numbers:
            continue
        key = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
        out[key] = numbers[0] if len(numbers) == 1 else numbers
    return out


def _parse_ranges_tables(soup: BeautifulSoup) -> dict:
    """
    table.ranges-table (4 of them under #analysis: Compounded Sales Growth,
    Compounded Profit Growth, Stock Price CAGR, Return on Equity) ->
    {metric_key: {period_key: float}}.
    """
    out: dict = {}
    for table in soup.find_all("table", class_="ranges-table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        title = rows[0].get_text(strip=True)
        if not title:
            continue
        metric_key = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")
        periods: dict = {}
        for row in rows[1:]:
            cells = row.find_all("td")
            if len(cells) < 2:
                continue
            period_label = cells[0].get_text(strip=True).rstrip(":")
            value = _clean_number(cells[1].get_text())
            if value is None:
                continue
            period_key = re.sub(r"[^a-z0-9]+", "_", period_label.lower()).strip("_")
            periods[period_key] = value
        if periods:
            out[metric_key] = periods
    return out


def _parse_balance_sheet_debt_equity(soup: BeautifulSoup) -> Optional[float]:
    """
    #balance-sheet table -> debt_to_equity from the most recent column:
    latest Borrowings / (latest Equity Capital + Reserves). None if any of
    the three rows, or a numeric latest-column value, can't be found.
    """
    section = soup.find(id="balance-sheet")
    if not section:
        return None
    table = section.find("table")
    if not table:
        return None

    row_values: dict = {}
    for row in table.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) < 2:
            continue
        label = cells[0].get_text(strip=True).rstrip("+").strip()
        last_value = _clean_number(cells[-1].get_text())
        if last_value is not None:
            row_values[label] = last_value

    equity_capital = row_values.get("Equity Capital")
    reserves = row_values.get("Reserves")
    borrowings = row_values.get("Borrowings")
    if equity_capital is None or reserves is None or borrowings is None:
        return None
    total_equity = equity_capital + reserves
    if total_equity <= 0:
        return None
    return round(borrowings / total_equity, 3)


def _parse_shareholding(soup: BeautifulSoup) -> dict:
    """
    #shareholding table -> promoter holding trend.
    Returns: promoter_holding (latest quarter, %), promoter_holding_1q_ago,
    promoter_holding_4q_ago, promoter_holding_change_yoy (latest - 4 quarters
    ago, in percentage points — negative means promoters have been
    reducing their stake). Missing pieces are simply absent from the dict,
    never fabricated.
    """
    out: dict = {}
    section = soup.find(id="shareholding")
    if not section:
        return out
    table = section.find("table")
    if not table:
        return out

    promoter_row = None
    for row in table.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if cells and cells[0].get_text(strip=True).rstrip("+").strip() == "Promoters":
            promoter_row = cells[1:]
            break
    if not promoter_row:
        return out

    values = [_clean_number(c.get_text()) for c in promoter_row]
    values = [v for v in values if v is not None]
    if not values:
        return out

    out["promoter_holding"] = values[-1]
    if len(values) >= 2:
        out["promoter_holding_1q_ago"] = values[-2]
    if len(values) >= 5:
        out["promoter_holding_4q_ago"] = values[-5]
        out["promoter_holding_change_yoy"] = round(values[-1] - values[-5], 2)
    return out


def parse_fundamentals_page(html: str) -> dict:
    """
    Pure parsing function — no network. Takes a Screener company page's raw
    HTML and returns whatever it could extract. Never raises: a field that
    can't be found is simply absent from the result, not an error.
    """
    soup = BeautifulSoup(html, "html.parser")
    result: dict = {}

    top = _parse_top_ratios(soup)
    result["market_cap_cr"] = top.get("market_cap")
    result["current_price"] = top.get("current_price")
    result["pe"] = top.get("stock_p_e")
    result["book_value"] = top.get("book_value")
    result["dividend_yield"] = top.get("dividend_yield")
    result["roce"] = top.get("roce")
    result["roe"] = top.get("roe")

    ranges = _parse_ranges_tables(soup)
    sales_growth = ranges.get("compounded_sales_growth", {})
    profit_growth = ranges.get("compounded_profit_growth", {})
    result["sales_growth_3yr"] = sales_growth.get("3_years")
    result["sales_growth_5yr"] = sales_growth.get("5_years")
    result["sales_growth_10yr"] = sales_growth.get("10_years")
    result["sales_growth_ttm"] = sales_growth.get("ttm")
    result["profit_growth_3yr"] = profit_growth.get("3_years")
    result["profit_growth_5yr"] = profit_growth.get("5_years")

    result["debt_to_equity"] = _parse_balance_sheet_debt_equity(soup)

    result.update(_parse_shareholding(soup))

    # Drop keys that came back empty so callers never mistake "not found"
    # for "found and genuinely zero".
    return {k: v for k, v in result.items() if v is not None}


def fetch_fundamentals(symbol: str, session: Optional[requests.Session] = None,
                        timeout: int = 15) -> Optional[dict]:
    """
    Fetches and parses one company's public Screener page. Returns None —
    never raises — on any network or parse failure, so a fundamentals
    outage degrades to "no fundamental grounding this cycle" rather than
    blocking signal generation the way a Kite outage blocks everything
    (fundamentals are supporting context, not the trade trigger).

    symbol: NSE/BSE trading symbol as used elsewhere in this codebase
    (universe.csv's Ticker column, e.g. "RELIANCE", "TATASTEEL") — this is
    also Screener's own URL slug for the great majority of listed
    companies, since both ultimately key off the same exchange symbol.
    """
    sess = session or requests.Session()
    sess.headers.update(_HEADERS)
    try:
        resp = sess.get(f"{BASE_URL}/company/{symbol}/", timeout=timeout)
        if resp.status_code == 404:
            log.info(f"Screener: no public page for {symbol} (404) — symbol "
                      f"may differ from its Screener slug, or isn't listed there.")
            return None
        resp.raise_for_status()
    except Exception as e:
        log.warning(f"Screener public-page fetch failed for {symbol}: {e}")
        return None

    try:
        data = parse_fundamentals_page(resp.text)
    except Exception as e:
        log.warning(f"Screener public-page parse failed for {symbol}: {e}")
        return None

    if not data:
        log.warning(f"Screener public page for {symbol} returned no parseable "
                     f"fundamentals — page layout may have changed.")
        return None

    return data

# ---------------------------------------------------------------------------
# Cached wrapper (ledger/db.py's kv_store-backed cache)
#
# Fundamentals move on a quarterly results cycle, not intraday. Hitting
# Screener's public page on every research cycle (up to ~7x/day, across the
# whole watchlist) would be pointless load for data that barely changes
# within a week. This wraps fetch_fundamentals() with the existing kv_store
# cache in ledger/db.py, refetching only when the cached snapshot is older
# than max_age_days or missing -- and falling back to a stale cache entry
# (rather than None) if a live refetch fails, so a transient Screener
# outage doesn't strip fundamental grounding from signal generation.
# ---------------------------------------------------------------------------

DEFAULT_MAX_AGE_DAYS = 7

_TS_FORMAT = "%Y-%m-%d %H:%M:%S"  # matches ledger.db.now_ist()


def get_fundamentals_cached(
    symbol: str,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    session: Optional[requests.Session] = None,
) -> Optional[dict]:
    """
    Returns fundamentals for `symbol`, preferring a fresh cache hit over a
    live Screener fetch, and falling back to a stale cache entry if a live
    fetch fails (better a few-days-old ROCE than none at all -- signal
    generation should degrade gracefully, not silently lose fundamental
    context because one HTTP call had a bad day).

    The ledger.db import is deferred to call time rather than module load,
    so this module has no hard dependency on a live ledger DB file -- it
    stays importable and unit-testable on its own.
    """
    from datetime import datetime, timedelta

    from ledger.db import get_cached_fundamentals, now_ist, save_fundamentals_cache

    cached = get_cached_fundamentals(symbol)
    stale_data, stale_updated_at = (None, None)
    if cached is not None:
        data, updated_at = cached
        stale_data, stale_updated_at = data, updated_at
        try:
            age = datetime.strptime(now_ist(), _TS_FORMAT) - datetime.strptime(
                updated_at, _TS_FORMAT
            )
        except Exception:
            age = None
        if age is not None and age <= timedelta(days=max_age_days):
            return data

    fresh = fetch_fundamentals(symbol, session=session)
    if fresh is not None:
        save_fundamentals_cache(symbol, fresh)
        return fresh

    if stale_data is not None:
        log.info(
            f"Screener fetch failed for {symbol}; using stale cached "
            f"fundamentals from {stale_updated_at}."
        )
        return stale_data

    return None

