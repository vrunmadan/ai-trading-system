"""
Buy-and-hold index benchmark for the paper (or live) book.

Question it answers: would the same capital, put into the regime index
(default NIFTY SMLCAP 250) on the day the book took its first trade and
simply held, be worth more or less than the book is today?

  benchmark_value = CAPITAL * index_close_today / index_close_on_start_date
  book_value      = CAPITAL + realised P&L + unrealised P&L (marked at LTP)

Design choices, stated so the comparison stays honest:
  - Start date = the book's first trade entry date (fills not marked
    NOT_EXECUTED), or BENCHMARK_START_DATE if set. Both sides start from the
    same capital on the same day.
  - The index is "bought" at the CLOSE of the start date. The book's first
    entry was intraday, so day one is approximate; over weeks it washes out.
  - The index is a price index: no dividends, no fund expense ratio. A real
    Smallcap 250 index fund would track slightly differently.
  - The start date and baseline close are frozen in kv_store on first
    computation, so the benchmark doesn't drift if history is refetched.
  - Never invents data: if the index can't be fetched, returns None and the
    daily summary says so instead of showing a number.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta

log = logging.getLogger(__name__)

BENCHMARK_INDEX_SYMBOL = os.getenv(
    "BENCHMARK_INDEX_SYMBOL", os.getenv("REGIME_INDEX_SYMBOL", "NIFTY SMLCAP 250")
)


def _capital() -> float:
    return float(os.getenv("TOTAL_CAPITAL_INR", "1000000"))


def _baseline_key(mode: str) -> str:
    return f"benchmark_baseline:{mode}:{BENCHMARK_INDEX_SYMBOL}"


# ---------------------------------------------------------------------------
# Book side
# ---------------------------------------------------------------------------
def get_book_start_date(mode: str) -> date | None:
    """First trade entry date in this book (IST wall-clock string in the DB)."""
    override = os.getenv("BENCHMARK_START_DATE", "").strip()
    if override:
        return datetime.strptime(override, "%Y-%m-%d").date()
    from ledger.db import get_db
    with get_db() as conn:
        row = conn.execute(
            "SELECT MIN(entry_time) AS first FROM trades "
            "WHERE mode = ? AND entry_time IS NOT NULL "
            "AND COALESCE(fill_status, 'CONFIRMED') != 'NOT_EXECUTED'",
            (mode,),
        ).fetchone()
    first = row["first"] if row else None
    if not first:
        return None
    return datetime.strptime(str(first)[:10], "%Y-%m-%d").date()


def get_book_value(mode: str, ltp_fn=None) -> tuple[float, int]:
    """(book value, number of open positions that could not be priced).

    Same formula as the portfolio risk gate: CAPITAL + all-time realised P&L
    + unrealised P&L. An open position whose LTP can't be fetched is carried
    at cost (0 unrealised) and counted, so the summary can say so.
    """
    from ledger.db import get_all_time_pnl, get_open_positions
    if ltp_fn is None:
        from trader.kite_client import get_ltp as ltp_fn
    value = _capital() + get_all_time_pnl(mode)
    unpriced = 0
    for p in get_open_positions(mode=mode):
        qty = int(p["quantity"])
        cost = float(p["entry_price"]) * qty
        try:
            value += ltp_fn(p["ticker"], p.get("exchange") or "NSE") * qty - cost
        except Exception as e:
            unpriced += 1
            log.warning(f"Benchmark: no LTP for {p['ticker']}, carried at cost: {e}")
    return value, unpriced


# ---------------------------------------------------------------------------
# Index side
# ---------------------------------------------------------------------------
def _resolve_token(kite) -> int | None:
    from researcher.regime_classifier import _resolve_index_token
    try:
        rows = kite.instruments("NSE")
    except Exception as e:
        log.warning(f"Benchmark: instruments() failed: {e}")
        rows = []
    return _resolve_index_token(rows, BENCHMARK_INDEX_SYMBOL)


def fetch_index_closes(kite, token: int, start: date, end: date) -> list[tuple[date, float]]:
    """Completed daily closes from start..end inclusive, oldest first."""
    from trader.kite_client import drop_incomplete_today_bar
    hist = kite.historical_data(
        token, start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"), "day",
        continuous=False, oi=False,
    )
    hist = drop_incomplete_today_bar(hist)
    out = []
    for bar in hist or []:
        d = bar.get("date")
        if isinstance(d, datetime):
            d = d.date()
        elif isinstance(d, str):
            d = datetime.strptime(d[:10], "%Y-%m-%d").date()
        if d is None or bar.get("close") is None:
            continue
        out.append((d, float(bar["close"])))
    out.sort(key=lambda x: x[0])
    return out


def _max_drawdown_pct(values: list[float]) -> float:
    peak, worst = None, 0.0
    for v in values:
        peak = v if peak is None or v > peak else peak
        if peak:
            worst = min(worst, (v - peak) / peak * 100)
    return worst


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------
def compute_benchmark(mode: str = "PAPER", kite=None, ltp_fn=None, today: date | None = None) -> dict | None:
    """Compare the book with buy-and-hold of the index. None if not computable
    (no trades yet, or index data unavailable). Never raises."""
    try:
        from ledger.db import kv_get, kv_set, get_db
        today = today or date.today()

        frozen = kv_get(_baseline_key(mode))
        baseline = json.loads(frozen[0]) if frozen else None

        start = (datetime.strptime(baseline["start_date"], "%Y-%m-%d").date()
                 if baseline else get_book_start_date(mode))
        if start is None:
            return {"status": "NO_TRADES"}

        if kite is None:
            from trader.kite_client import get_kite_client
            kite = get_kite_client()
        token = _resolve_token(kite)
        if token is None:
            return {"status": "NO_INDEX", "detail": f"index {BENCHMARK_INDEX_SYMBOL} not found"}

        closes = fetch_index_closes(kite, token, start - timedelta(days=7), today)
        on_or_after = [c for c in closes if c[0] >= start]
        if not on_or_after:
            return {"status": "NO_INDEX", "detail": "no index closes since start date"}

        if baseline is None:
            base_date, base_close = on_or_after[0]
            baseline = {"start_date": start.isoformat(),
                        "base_date": base_date.isoformat(),
                        "base_close": base_close,
                        "symbol": BENCHMARK_INDEX_SYMBOL}
            try:
                kv_set(_baseline_key(mode), json.dumps(baseline))
            except Exception as e:
                log.warning(f"Benchmark: could not freeze baseline: {e}")
        base_close = float(baseline["base_close"])

        last_date, last_close = on_or_after[-1]
        capital = _capital()
        bench_value = capital * last_close / base_close
        bench_series = [capital * c / base_close for d, c in on_or_after
                        if d >= datetime.strptime(baseline["base_date"], "%Y-%m-%d").date()]

        book_value, unpriced = get_book_value(mode, ltp_fn=ltp_fn)

        with get_db() as conn:
            row = conn.execute(
                "SELECT peak_value FROM portfolio_peak WHERE mode = ?", (mode,)
            ).fetchone()
        book_peak = max(float(row["peak_value"]) if row else capital, book_value, capital)

        result = {
            "status": "OK",
            "mode": mode,
            "symbol": baseline["symbol"],
            "start_date": baseline["start_date"],
            "base_date": baseline["base_date"],
            "base_close": base_close,
            "index_date": last_date.isoformat(),
            "index_close": last_close,
            "capital": capital,
            "book_value": book_value,
            "book_return_pct": (book_value / capital - 1) * 100,
            "book_drawdown_pct": (book_value - book_peak) / book_peak * 100,
            "bench_value": bench_value,
            "bench_return_pct": (bench_value / capital - 1) * 100,
            "bench_max_drawdown_pct": _max_drawdown_pct(bench_series),
            "unpriced_positions": unpriced,
        }
        result["edge_pct_points"] = result["book_return_pct"] - result["bench_return_pct"]
        result["edge_inr"] = book_value - bench_value
        _save_snapshot(result, today)
        return result
    except Exception as e:
        log.error(f"Benchmark computation failed: {e}", exc_info=True)
        return {"status": "ERROR", "detail": str(e)[:200]}


def _save_snapshot(r: dict, today: date) -> None:
    """One row per day per book, so the comparison can be charted later."""
    try:
        from ledger.db import get_db
        with get_db() as conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS benchmark_daily (
                    day TEXT NOT NULL, mode TEXT NOT NULL, symbol TEXT NOT NULL,
                    book_value REAL, bench_value REAL, index_close REAL,
                    index_date TEXT, PRIMARY KEY (day, mode, symbol))"""
            )
            conn.execute(
                """INSERT INTO benchmark_daily
                   (day, mode, symbol, book_value, bench_value, index_close, index_date)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(day, mode, symbol) DO UPDATE SET
                     book_value=excluded.book_value, bench_value=excluded.bench_value,
                     index_close=excluded.index_close, index_date=excluded.index_date""",
                (today.isoformat(), r["mode"], r["symbol"], r["book_value"],
                 r["bench_value"], r["index_close"], r["index_date"]),
            )
            conn.commit()
    except Exception as e:
        log.warning(f"Benchmark: snapshot not saved: {e}")


def format_benchmark_block(r: dict | None) -> str:
    if not r:
        return ""
    status = r.get("status")
    if status == "NO_TRADES":
        return ("Benchmark vs buy-and-hold index:\n"
                "  Not started - the book has no trades yet.")
    if status != "OK":
        return ("Benchmark vs buy-and-hold index:\n"
                f"  Unavailable today ({r.get('detail', status)}). No number shown rather than a guess.")
    sign = lambda x: f"{x:+.1f}%"
    verdict = ("AHEAD of" if r["edge_inr"] > 0 else "BEHIND" if r["edge_inr"] < 0 else "level with")
    lines = [
        f"Benchmark - {r['mode'].lower()} book vs buy-and-hold {r['symbol']}",
        f"  Since {r['start_date']}, both from Rs {r['capital']:,.0f}:",
        f"  Book:       Rs {r['book_value']:,.0f}  ({sign(r['book_return_pct'])}; "
        f"{r['book_drawdown_pct']:.1f}% from peak)",
        f"  Index B&H:  Rs {r['bench_value']:,.0f}  ({sign(r['bench_return_pct'])}; "
        f"worst drawdown {r['bench_max_drawdown_pct']:.1f}%)",
        f"  The book is {verdict} the index by Rs {abs(r['edge_inr']):,.0f} "
        f"({r['edge_pct_points']:+.1f} pts).",
        f"  Index {r['base_close']:,.2f} on {r['base_date']} -> {r['index_close']:,.2f} on {r['index_date']}"
        " (price index: no dividends or fund costs).",
    ]
    if r.get("unpriced_positions"):
        lines.append(f"  Note: {r['unpriced_positions']} open position(s) could not be priced "
                     "and are carried at cost.")
    return "\n".join(lines)
