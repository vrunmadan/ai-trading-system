"""
universe/discovery.py
---------------------
Weekly DISCOVERY list: liquid, fundamentally strong NSE stocks that sit
OUTSIDE the core universe (Nifty Midcap 150 + Smallcap 250 + Microcap 250,
i.e. roughly market-cap ranks 101-750) and outside the large caps — the
"unheard-of multibagger" pool Varun asked to have in scope (2026-09-23).

Why not the old universe_refresher.py: it runs a Screener *screen* across
all listings, which needs a Screener premium login this deployment doesn't
have (SCREENER_EMAIL/PASSWORD unset), so it has never produced a list.
This job needs no premium: Kite gives the full NSE listing, quotes and
turnover; Screener's PUBLIC company page gives fundamentals per stock.

Pipeline (each stage only sees the previous stage's survivors, so the
expensive calls happen on the smallest set):
  1. Kite NSE instruments, EQ series only (no BE/T2T, SME, ETFs-with-suffix),
     minus the core universe and the large-cap exclusion list.
  2. Kite quotes (500/call): drop 2%/5% circuit-band names — a stock pinned
     at its lower circuit can't be sold, so a stop is meaningless — and use
     today's turnover as a cheap pre-screen.
  3. Kite 20-day average turnover >= MIN_DAILY_TURNOVER_INR — the SAME floor
     microstructure_checks() enforces at approval time, so nothing is
     discovered that the approval path would then refuse to trade.
  4. Screener public page: market cap in range, the standard fundamental
     gate (incl. promoter pledge), plus a stricter quality bar, and — unlike
     the core-universe gate — FAIL CLOSED: an unknown name with no data is
     not added.
  5. Keep the top DISCOVERY_MAX_NAMES by return on capital.

The result is stored in the ledger kv_store (key "discovery_universe") and
merged into the scan by universe.loader.load_scan_universe(). universe.csv
is never edited (that stays a human decision). Every stage count is kept
so the weekly email says exactly where names dropped out.
"""

import json
import logging
import os
import time
from typing import Callable, Optional

log = logging.getLogger(__name__)

KV_KEY = "discovery_universe"

DISCOVERY_MIN_MCAP_CR = float(os.getenv("DISCOVERY_MIN_MCAP_CR", "300"))
DISCOVERY_MAX_MCAP_CR = float(os.getenv("DISCOVERY_MAX_MCAP_CR", "15000"))
DISCOVERY_MIN_ROCE = float(os.getenv("DISCOVERY_MIN_ROCE", "15"))
DISCOVERY_MIN_ROE_FINANCIALS = float(os.getenv("DISCOVERY_MIN_ROE_FINANCIALS", "12"))
DISCOVERY_MIN_SALES_GROWTH = float(os.getenv("DISCOVERY_MIN_SALES_GROWTH", "10"))
DISCOVERY_MIN_CIRCUIT_BAND = float(os.getenv("DISCOVERY_MIN_CIRCUIT_BAND", "10"))
DISCOVERY_MAX_NAMES = int(os.getenv("DISCOVERY_MAX_NAMES", "150"))
DISCOVERY_SCREENER_DELAY = float(os.getenv("DISCOVERY_SCREENER_DELAY", "1.5"))
DISCOVERY_KITE_DELAY = float(os.getenv("DISCOVERY_KITE_DELAY", "0.35"))

_HERE = os.path.dirname(os.path.abspath(__file__))
LARGECAP_EXCLUDE_PATH = os.path.join(_HERE, "largecap_exclude.csv")


def _load_largecaps() -> set[str]:
    import csv
    try:
        with open(LARGECAP_EXCLUDE_PATH, newline="", encoding="utf-8") as f:
            return {r["Ticker"].strip().upper() for r in csv.DictReader(f) if r.get("Ticker")}
    except Exception as e:
        log.warning(f"Discovery: could not read large-cap exclusion list: {e}")
        return set()


def _circuit_band_pct(q: dict) -> Optional[float]:
    """Half-width of the day's circuit band as % of previous close
    (a 10% band -> 10.0). None if the quote lacks the fields."""
    try:
        up, lo = float(q["upper_circuit_limit"]), float(q["lower_circuit_limit"])
        prev = float((q.get("ohlc") or {}).get("close") or q["last_price"])
        if prev <= 0 or up <= 0 or lo <= 0:
            return None
        return (up - lo) / (2 * prev) * 100
    except Exception:
        return None


def _quality_ok(fund: dict, sector: str) -> tuple[bool, str]:
    """Stricter than the core gate, and fail-closed on missing data."""
    from researcher.signal_generator import _is_financial

    mcap = fund.get("market_cap_cr")
    if mcap is None:
        return False, "no market cap"
    if not (DISCOVERY_MIN_MCAP_CR <= mcap <= DISCOVERY_MAX_MCAP_CR):
        return False, f"market cap ₹{mcap:,.0f}Cr outside range"
    if _is_financial(sector):
        roe = fund.get("roe")
        if roe is None or roe < DISCOVERY_MIN_ROE_FINANCIALS:
            return False, f"ROE {roe} below {DISCOVERY_MIN_ROE_FINANCIALS}"
    else:
        roce = fund.get("roce")
        if roce is None or roce < DISCOVERY_MIN_ROCE:
            return False, f"ROCE {roce} below {DISCOVERY_MIN_ROCE}"
    growth = fund.get("sales_growth_3yr")
    if growth is None:
        growth = fund.get("sales_growth_5yr")
    if growth is None or growth < DISCOVERY_MIN_SALES_GROWTH:
        return False, f"sales growth {growth} below {DISCOVERY_MIN_SALES_GROWTH}"
    return True, "ok"


def build_discovery_list(
    kite,
    fetch_fundamentals: Optional[Callable[[str], Optional[dict]]] = None,
    sleep: Callable[[float], None] = time.sleep,
    min_turnover_inr: Optional[float] = None,
) -> dict:
    """Pure-ish core (all I/O injected) so it's unit-testable without Kite."""
    from datetime import datetime, timedelta

    from researcher.signal_generator import _passes_fundamental_gate
    from universe.loader import load_universe

    if fetch_fundamentals is None:
        from fundamentals.screener_public import get_fundamentals_cached as fetch_fundamentals
    if min_turnover_inr is None:
        from trader.kite_client import MIN_DAILY_TURNOVER_INR as min_turnover_inr

    stats: dict = {}
    core = {e.ticker for e in load_universe()}
    excluded = core | _load_largecaps()

    # 1. Listing
    rows = kite.instruments("NSE")
    cands = {}
    for r in rows:
        sym = str(r.get("tradingsymbol", "")).upper()
        if (r.get("segment", "NSE") == "NSE" and r.get("instrument_type") == "EQ"
                and sym and "-" not in sym and sym not in excluded):
            cands[sym] = r
    stats["listed_outside_core"] = len(cands)

    # 2. Quotes: circuit band + cheap turnover pre-screen
    syms = sorted(cands)
    band_ok, pre_ok = [], []
    for i in range(0, len(syms), 500):
        keys = [f"NSE:{s}" for s in syms[i:i + 500]]
        try:
            quotes = kite.quote(keys)
        except Exception as e:
            log.warning(f"Discovery: quote batch failed ({e}) — skipping {len(keys)} names")
            quotes = {}
        for k, q in quotes.items():
            sym = k.split(":", 1)[1]
            band = _circuit_band_pct(q)
            if band is None or band < DISCOVERY_MIN_CIRCUIT_BAND - 0.5:
                continue
            band_ok.append(sym)
            today_turnover = float(q.get("volume") or 0) * float(q.get("last_price") or 0)
            # 30% of the floor on one day is a loose, cheap filter — the real
            # test is the 20-day average below.
            if today_turnover >= 0.3 * min_turnover_inr:
                pre_ok.append(sym)
        sleep(DISCOVERY_KITE_DELAY)
    stats["circuit_band_ok"] = len(band_ok)
    stats["turnover_prescreen_ok"] = len(pre_ok)

    # 3. 20-day average turnover
    liquid = []
    to_d = datetime.now()
    from_d = to_d - timedelta(days=35)
    for sym in pre_ok:
        try:
            candles = kite.historical_data(int(cands[sym]["instrument_token"]), from_d, to_d, "day")
            recent = candles[-20:]
            if recent:
                avg = sum(c["volume"] * c["close"] for c in recent) / len(recent)
                if avg >= min_turnover_inr:
                    liquid.append((sym, avg))
        except Exception as e:
            log.debug(f"Discovery: history failed for {sym}: {e}")
        sleep(DISCOVERY_KITE_DELAY)
    stats["liquid"] = len(liquid)

    # 4. Fundamentals (fail closed)
    passed, rejections = [], {}
    for sym, avg in liquid:
        t0 = time.monotonic()
        fund = fetch_fundamentals(sym)
        if time.monotonic() - t0 > 0.2:      # was a live Screener hit, be polite
            sleep(DISCOVERY_SCREENER_DELAY)
        if not fund:
            rejections["no fundamentals data"] = rejections.get("no fundamentals data", 0) + 1
            continue
        sector = (fund.get("broad_sector") or "UNKNOWN").strip().upper()
        ok, why = _passes_fundamental_gate(fund, sector=sector)
        if ok:
            ok, why = _quality_ok(fund, sector)
        if not ok:
            key = why.split(" ")[0] if why else "rejected"
            rejections[key] = rejections.get(key, 0) + 1
            continue
        score = fund.get("roe") if sector == "FINANCIAL SERVICES" else fund.get("roce")
        passed.append({
            "ticker": sym,
            "company": str(cands[sym].get("name") or sym).title(),
            "sector": sector,
            "market_cap_cr": fund.get("market_cap_cr"),
            "exchange": "NSE",
            "notes": "Discovery",
            "avg_turnover_cr": round(avg / 1e7, 2),
            "quality": score,
        })
    stats["fundamentals_ok"] = len(passed)
    stats["rejections"] = rejections

    # 5. Cap
    passed.sort(key=lambda e: (e["quality"] or 0), reverse=True)
    entries = passed[:DISCOVERY_MAX_NAMES]
    stats["kept"] = len(entries)
    return {"entries": entries, "stats": stats}


def load_discovery_entries() -> list[dict]:
    """Stored list, or [] if none/unreadable."""
    try:
        from ledger.db import kv_get
        row = kv_get(KV_KEY)
        if not row:
            return []
        return json.loads(row[0]).get("entries", [])
    except Exception as e:
        log.warning(f"Discovery list unreadable: {e}")
        return []


def run_and_email() -> dict:
    """Scheduled entry point: build, store, email a stage-by-stage report."""
    from alerts.gmail_alert import send_plain_email
    from ledger.db import kv_get, kv_set, now_ist
    from trader.kite_client import get_kite_client

    previous = {e["ticker"] for e in load_discovery_entries()}
    try:
        result = build_discovery_list(get_kite_client())
    except Exception as e:
        log.error(f"Discovery run failed: {e}", exc_info=True)
        send_plain_email("⚠️ Weekly discovery list FAILED",
                         f"The discovery job raised: {e}\nLast week's list stays in use.")
        return {"error": str(e)}

    entries, st = result["entries"], result["stats"]
    if not entries and previous:
        # A total wipe-out is far more likely a data outage than a real
        # market-wide collapse in quality — keep last week's list.
        send_plain_email("⚠️ Weekly discovery list empty — kept last week's",
                         json.dumps(st, indent=2))
        return result

    kv_set(KV_KEY, json.dumps({"generated_at": now_ist(), "entries": entries, "stats": st}))
    now = {e["ticker"] for e in entries}
    added, dropped = sorted(now - previous), sorted(previous - now)
    lines = [
        f"Discovery list rebuilt {now_ist()} — {len(entries)} names now in the scan",
        "",
        "Funnel:",
        f"  NSE EQ listings outside core + large caps : {st['listed_outside_core']}",
        f"  circuit band >= {DISCOVERY_MIN_CIRCUIT_BAND:.0f}%                    : {st['circuit_band_ok']}",
        f"  passed today's-turnover pre-screen          : {st['turnover_prescreen_ok']}",
        f"  20d avg turnover >= floor                   : {st['liquid']}",
        f"  fundamentals + quality bar                  : {st['fundamentals_ok']}",
        f"  kept (cap {DISCOVERY_MAX_NAMES})                           : {st['kept']}",
        f"  rejection reasons: {st.get('rejections')}",
        "",
        f"Added ({len(added)}): {', '.join(added) or '—'}",
        f"Dropped ({len(dropped)}): {', '.join(dropped) or '—'}",
        "",
        "Top 25 by return on capital:",
    ]
    for e in entries[:25]:
        lines.append(f"  {e['ticker']:<12} {e['sector'][:22]:<22} mcap ₹{(e['market_cap_cr'] or 0):>8,.0f}Cr  "
                     f"RoC {e['quality']}  turnover ₹{e['avg_turnover_cr']}Cr/day")
    send_plain_email(f"🔎 Discovery list: {len(entries)} names (+{len(added)} / -{len(dropped)})",
                     "\n".join(lines))
    return result
