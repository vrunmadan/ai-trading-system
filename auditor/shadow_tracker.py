"""
Shadow price tracker — grades QC_BLOCKED / QC_ERROR signals against what
actually happened to the price, without ever placing an order.

Why this exists:
  Before this, a QC_BLOCKED signal recorded the researcher's scores and QC's
  rationale but never the price at the moment it was blocked. The weekly
  Auditor's "missed opportunity review" could describe patterns ("QC blocked
  five high-technical signals citing unverifiable fundamentals") but could
  never say whether blocking them was actually right, because there was
  nothing to compare the price to afterward.

What it does:
  Runs daily (Mon-Fri, after the EOD sweep) and, for every QC_BLOCKED /
  QC_ERROR signal that has a recorded price_at_signal, checks the current
  LTP at three fixed horizons — 1, 3, and 5 calendar days after the signal
  was created — and records a direction-adjusted hypothetical return.
  Each (signal, horizon) pair is checked at most once (see
  ledger/db.py:get_signals_needing_shadow_check / record_shadow_check).

This is strictly a read of the live price. No order is placed, no sizing or
capital is touched, and nothing here can affect PAPER_MODE or live trading.
"""

import logging

log = logging.getLogger(__name__)

# Calendar days, not trading days — simpler, and Kite's LTP still returns
# the last traded price on a non-trading day, so a weekend horizon just
# reads as "unchanged since Friday's close" rather than erroring.
HORIZONS_DAYS = (1, 3, 5)


def run_shadow_checks() -> int:
    """
    Check every QC_BLOCKED/QC_ERROR signal due for a shadow check at any of
    HORIZONS_DAYS and record the result.

    Safe to call daily: signals that already have a given horizon recorded
    are skipped (get_signals_needing_shadow_check excludes them), and
    record_shadow_check is idempotent per (signal_id, horizon_days).

    Returns the number of shadow checks recorded, for logging/testing.
    """
    from ledger.db import get_signals_needing_shadow_check, record_shadow_check
    from trader.kite_client import get_ltp

    total_recorded = 0

    for horizon in HORIZONS_DAYS:
        try:
            due = get_signals_needing_shadow_check(horizon)
        except Exception as e:
            log.error(f"Could not fetch signals due for a {horizon}d shadow check: {e}")
            continue

        if not due:
            continue

        log.info(f"{len(due)} signal(s) due for a {horizon}d shadow check.")

        for row in due:
            signal_id = row["id"]
            ticker = row["ticker"]
            exchange = row.get("exchange") or "NSE"
            direction = row.get("direction") or "BUY"
            entry_price = row.get("price_at_signal")

            if not entry_price or entry_price <= 0:
                continue

            try:
                price_now = get_ltp(ticker, exchange)
            except Exception as e:
                log.warning(f"{ticker} (signal {signal_id}): LTP fetch failed for "
                            f"{horizon}d shadow check, will retry next run: {e}")
                continue

            if not price_now or price_now <= 0:
                continue

            raw_return_pct = (price_now - entry_price) / entry_price * 100
            # SELL/short signals move opposite to price — a fall is a gain.
            return_pct = raw_return_pct if direction == "BUY" else -raw_return_pct

            try:
                record_shadow_check(
                    signal_id=signal_id,
                    horizon_days=horizon,
                    price_at_check=price_now,
                    return_pct=return_pct,
                    notes=(
                        f"{row.get('status')} ({row.get('qc_verdict')}): "
                        f"₹{entry_price:.2f} → ₹{price_now:.2f} "
                        f"({return_pct:+.1f}% if it had been taken)"
                    ),
                )
                total_recorded += 1
            except Exception as e:
                log.error(f"Could not record {horizon}d shadow check for "
                          f"signal {signal_id} ({ticker}): {e}")

    if total_recorded:
        log.info(f"Shadow price checks: recorded {total_recorded} result(s).")

    return total_recorded
