"""
Monitor — daily check on every open position (runs at 15:35 IST, after market close).

For each open trade:
  1. Fetches current LTP from Kite Connect
  2. Checks against stop-loss (entry_price × (1 - LONG_STOP_LOSS_PCT / 100))
  3. Checks if the regime has flipped against the entry thesis
  4. Writes result to position_checks table
  5. Sends a Gmail alert if status is INVALIDATED or WEAKENING

Stop-loss default: 7% below entry price for long positions.
Intent is to replace this with 2× ATR at entry time once ATR is logged
at signal creation. See TODO in schema.sql.

Regime invalidation map: if you entered in BULL but the regime is now
BEAR or CRASH, the thesis is gone — don't wait for the stop to be hit.
"""

import logging
import os

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
LONG_STOP_LOSS_PCT = float(os.getenv("LONG_STOP_LOSS_PCT", "7.0"))   # hard disaster stop from entry
TRAILING_STOP_PCT = float(os.getenv("TRAILING_STOP_PCT", "20.0"))    # trailing stop from peak (backtest-validated)
WEAKENING_PCT = float(os.getenv("WEAKENING_ALERT_PCT", "3.0"))       # alert before stop

# Exit design (validated by streak_backtests/backtest.py, 10y, 20% trail beat 7%
# trail and time exits decisively; a profit target REDUCED expectancy so there is
# none): exit a long when price breaches the HIGHER of
#   (a) hard stop  = entry_price * (1 - LONG_STOP_LOSS_PCT/100), and
#   (b) trail stop = peak_price  * (1 - TRAILING_STOP_PCT/100),
# where peak_price = max(entry, every logged check price, today's price).
# Early on the hard stop binds; once a trade runs up, the trailing stop takes over.

# Regime combinations that invalidate a long thesis entered in key
REGIME_INVALIDATION_MAP: dict[str, list[str]] = {
    "bull":               ["bear", "extreme_fear_crash"],
    "sideways":           ["extreme_fear_crash"],
    "euphoria":           ["bear", "extreme_fear_crash"],
    "bear":               [],   # short bias — no longs to invalidate
    "extreme_fear_crash": [],
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _log_position_check(
    trade_id: int,
    price: float,
    thesis_status: str,
    notes: str,
) -> None:
    from ledger.db import get_db, now_ist

    try:
        with get_db() as conn:
            conn.execute(
                """
                INSERT INTO position_checks
                    (trade_id, checked_at, price_at_check, thesis_status, notes)
                VALUES (?, ?, ?, ?, ?)
                """,
                (trade_id, now_ist(), price, thesis_status, notes),
            )
    except Exception as e:
        log.error(f"Failed to log position check for trade {trade_id}: {e}")


def _send_monitor_email(text: str) -> None:
    try:
        from alerts.gmail_alert import send_plain_email
        send_plain_email(subject="📊 Daily Position Monitor", body=text)
    except Exception as e:
        log.error(f"Monitor email send failed: {e}")


def _peak_price(trade_id: int, entry_price: float, current_price: float) -> float:
    """Highest price seen over the life of the trade: max(entry, all prior
    daily checks, today). Used to anchor the trailing stop. No schema change —
    reuses the price_at_check column already logged each day."""
    peak = max(entry_price, current_price)
    try:
        from ledger.db import get_db
        with get_db() as conn:
            row = conn.execute(
                "SELECT MAX(price_at_check) AS hi FROM position_checks WHERE trade_id=?",
                (trade_id,),
            ).fetchone()
        if row and row["hi"] is not None:
            peak = max(peak, float(row["hi"]))
    except Exception as e:
        log.error(f"Could not read peak for trade {trade_id}: {e}")
    return peak


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def check_open_positions() -> None:
    """
    Fetches all open positions from the ledger, runs checks, logs results,
    and sends a Gmail alert for any action required.
    """
    from ledger.db import get_open_positions, get_db

    open_positions = get_open_positions()
    if not open_positions:
        log.info("No open positions to monitor.")
        return

    log.info(f"Checking {len(open_positions)} open position(s)...")

    # Classify current regime once for all positions
    current_regime: str | None = None
    try:
        from researcher.regime_classifier import classify_regime
        # apply_inertia=False: this EOD check runs outside the hourly research
        # cycle and shouldn't consume a slot in its regime-inertia smoothing —
        # see classify_regime()'s docstring.
        regime_reading = classify_regime(apply_inertia=False)
        current_regime = regime_reading.regime.value
        log.info(f"Current regime for position monitor: {current_regime}")
    except Exception as e:
        log.error(f"Regime classification failed in monitor (non-fatal): {e}")

    # Connect to Kite
    try:
        from trader.kite_client import get_kite_client
        kite = get_kite_client()
    except Exception as e:
        log.error(f"Kite connection failed in monitor — skipping price checks: {e}")
        return

    alerts: list[str] = []

    for pos in open_positions:
        trade_id = pos["id"]
        ticker = pos["ticker"]
        direction = pos["direction"]
        entry_price = float(pos["entry_price"])
        mode = pos["mode"]
        signal_id = pos.get("signal_id")
        mode_tag = "[PAPER] " if mode == "PAPER" else ""

        # ----------------------------------------------------------------
        # Step 1: Fetch current price
        # ----------------------------------------------------------------
        try:
            q = kite.ltp(f"NSE:{ticker}")
            current_price = q[f"NSE:{ticker}"]["last_price"]
        except Exception as e:
            notes = f"Could not fetch LTP: {e}"
            log.error(f"{ticker}: {notes}")
            _log_position_check(trade_id, 0.0, "UNKNOWN", notes)
            continue

        pnl_pct = (current_price - entry_price) / entry_price * 100

        # ----------------------------------------------------------------
        # Step 2: Stop-loss check (longs only for now)
        # Exit on the HIGHER of the hard stop (from entry) and the trailing
        # stop (from the peak price seen so far). See config notes at top.
        # ----------------------------------------------------------------
        hard_stop = entry_price * (1 - LONG_STOP_LOSS_PCT / 100)
        peak_price = _peak_price(trade_id, entry_price, current_price)
        trail_stop = peak_price * (1 - TRAILING_STOP_PCT / 100)
        stop_price = max(hard_stop, trail_stop)          # effective stop
        stop_kind = "trailing" if trail_stop >= hard_stop else "hard"
        stop_triggered = direction == "BUY" and current_price <= stop_price

        # ----------------------------------------------------------------
        # Step 3: Regime invalidation check
        # ----------------------------------------------------------------
        regime_invalidated = False
        entry_regime: str | None = None

        if current_regime and signal_id:
            try:
                with get_db() as conn:
                    row = conn.execute(
                        "SELECT regime FROM signals WHERE id=?", (signal_id,)
                    ).fetchone()
                if row:
                    entry_regime = row["regime"]
                    invalidating = REGIME_INVALIDATION_MAP.get(entry_regime, [])
                    if current_regime in invalidating:
                        regime_invalidated = True
            except Exception as e:
                log.error(f"Could not look up entry regime for trade {trade_id}: {e}")

        # ----------------------------------------------------------------
        # Step 4: Determine thesis status
        # ----------------------------------------------------------------
        if stop_triggered or regime_invalidated:
            thesis_status = "INVALIDATED"
            reasons = []
            if stop_triggered:
                reasons.append(
                    f"{stop_kind.capitalize()} stop triggered: ₹{current_price:.2f} ≤ "
                    f"₹{stop_price:.2f} ({pnl_pct:+.1f}%; peak ₹{peak_price:.2f}, "
                    f"trail {TRAILING_STOP_PCT:.0f}%)"
                )
            if regime_invalidated:
                reasons.append(
                    f"Regime flipped: entered in {entry_regime}, now {current_regime}"
                )
            notes = " | ".join(reasons)
            alerts.append(
                f"🚨 {mode_tag}EXIT ALERT — {ticker}\n"
                f"{notes}\n"
                f"Review and consider closing this position."
            )
            log.warning(f"{ticker} INVALIDATED: {notes}")

        elif direction == "BUY" and pnl_pct <= -WEAKENING_PCT:
            thesis_status = "WEAKENING"
            notes = (
                f"LTP ₹{current_price:.2f} vs entry ₹{entry_price:.2f} "
                f"({pnl_pct:+.1f}%). Approaching stop at ₹{stop_price:.2f}."
            )
            alerts.append(
                f"⚡ {mode_tag}WEAKENING — {ticker}\n"
                f"{notes}"
            )
            log.info(f"{ticker} WEAKENING: {notes}")

        else:
            thesis_status = "INTACT"
            notes = (
                f"LTP ₹{current_price:.2f} vs entry ₹{entry_price:.2f} "
                f"({pnl_pct:+.1f}%). Stop at ₹{stop_price:.2f}."
            )
            log.info(f"{ticker}: intact ({pnl_pct:+.1f}%)")

        _log_position_check(trade_id, current_price, thesis_status, notes)

    # ----------------------------------------------------------------
    # Send consolidated email alert (if any issues)
    # ----------------------------------------------------------------
    if alerts:
        header = f"Daily Position Monitor — {len(open_positions)} position(s)\n{'─' * 40}\n"
        _send_monitor_email(header + "\n\n".join(alerts))
    else:
        log.info(f"All {len(open_positions)} position(s) nominal — no alerts.")
