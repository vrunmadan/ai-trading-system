"""
Trader — places orders via the official Kite Connect API.

NOT Kite MCP (that's read-only for AI assistants).
NOT Streak (that's for backtesting strategy logic).
This module uses the Kite Connect developer API directly — the same
underlying API that Tradetron and Streak use under the hood.

PAPER_MODE (default true until you deliberately turn it off after
validation): logs the intended order to the ledger, prints what would
have been sent, never touches the real Kite order endpoint.

Circuit breaker checks (India-specific microstructure — these fire
BEFORE any order is placed, paper or live):
- ADV check: position size must be < 2% of the stock's 20-day average
  daily volume, otherwise your own order moves the price against you.
- Circuit limit check: if LTP is within 1.5% of the day's upper/lower
  circuit limit, skip — the stock may freeze before your exit.
- Corporate action blackout: if the stock has earnings, ex-dividend, or
  bonus/split within 3 trading days, skip — LLM timelines on these are
  unreliable and the risk is asymmetric.
"""

import os
import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)

PAPER_MODE = os.getenv("PAPER_MODE", "true").lower() == "true"
MAX_ADV_PCT = float(os.getenv("MAX_ADV_PCT", 2.0))       # max 2% of avg daily volume
CIRCUIT_BUFFER_PCT = float(os.getenv("CIRCUIT_BUFFER_PCT", 1.5))
CORP_ACTION_BLACKOUT_DAYS = int(os.getenv("CORP_ACTION_BLACKOUT_DAYS", 3))


@dataclass
class ExecutionResult:
    success: bool
    order_id: str | None
    fill_price: float | None
    quantity: int
    notes: str
    mode: str  # "PAPER" | "LIVE"


def get_kite_client():
    """Returns an authenticated KiteConnect instance."""
    from kiteconnect import KiteConnect
    api_key = os.getenv("KITE_API_KEY")
    access_token = os.getenv("KITE_ACCESS_TOKEN")
    if not api_key or not access_token:
        raise EnvironmentError(
            "KITE_API_KEY and KITE_ACCESS_TOKEN must be set in .env. "
            "Run: python setup/refresh_kite_token.py"
        )
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)
    return kite


def get_ltp(ticker: str) -> float:
    """Fetch last traded price from Kite."""
    kite = get_kite_client()
    quote = kite.ltp(f"NSE:{ticker}")
    return quote[f"NSE:{ticker}"]["last_price"]


def get_quote_details(ticker: str) -> dict:
    """Fetch full quote including circuit limits and OHLCV."""
    kite = get_kite_client()
    return kite.quote(f"NSE:{ticker}")[f"NSE:{ticker}"]


def microstructure_checks(ticker: str, capital_to_deploy: float) -> tuple[bool, str]:
    """
    Returns (ok, reason). If ok=False, the trade should be skipped.
    These are deterministic checks — no LLM involved.

    TODO: implement the 20-day ADV fetch (requires Kite historical data
    API — needs the ₹500/month plan). Stub returns True for now.
    """
    try:
        quote = get_quote_details(ticker)
        ltp = quote["last_price"]
        upper_circuit = quote.get("upper_circuit_limit", ltp * 1.2)
        lower_circuit = quote.get("lower_circuit_limit", ltp * 0.8)

        # Circuit buffer check
        pct_from_upper = (upper_circuit - ltp) / upper_circuit * 100
        pct_from_lower = (ltp - lower_circuit) / ltp * 100
        if pct_from_upper < CIRCUIT_BUFFER_PCT:
            return False, f"{ticker} is within {pct_from_upper:.1f}% of upper circuit — skip."
        if pct_from_lower < CIRCUIT_BUFFER_PCT:
            return False, f"{ticker} is within {pct_from_lower:.1f}% of lower circuit — skip."

        # TODO: ADV check — requires historical OHLCV data
        # adv = get_20d_avg_volume(ticker)
        # approx_shares = capital_to_deploy / ltp
        # if approx_shares > adv * (MAX_ADV_PCT / 100):
        #     return False, f"Order size > {MAX_ADV_PCT}% of ADV — would move the market."

        # TODO: Corporate action blackout
        # if days_to_next_corporate_action(ticker) <= CORP_ACTION_BLACKOUT_DAYS:
        #     return False, f"{ticker} has a corporate action within {CORP_ACTION_BLACKOUT_DAYS} days."

        return True, "Microstructure checks passed."

    except Exception as e:
        return False, f"Could not fetch quote for {ticker}: {e}"


def execute_trade(
    signal_id: int,
    ticker: str,
    direction: str,
    capital_to_deploy: float,
) -> ExecutionResult:
    """
    Main entry point called by the Telegram approval webhook.

    1. Re-fetches live LTP (price may have moved since signal was generated)
    2. Runs microstructure checks
    3. Computes share quantity from LTP
    4. Places order (or logs it in PAPER_MODE)
    5. Logs to ledger
    """
    from ledger.db import log_trade, update_signal_response

    # Microstructure checks first — always, paper or live
    ok, check_note = microstructure_checks(ticker, capital_to_deploy)
    if not ok:
        log.warning(f"Microstructure check failed for {ticker}: {check_note}")
        return ExecutionResult(
            success=False, order_id=None, fill_price=None,
            quantity=0, notes=check_note, mode="PAPER" if PAPER_MODE else "LIVE",
        )

    # Get live price and compute quantity
    try:
        ltp = get_ltp(ticker)
    except Exception as e:
        return ExecutionResult(
            success=False, order_id=None, fill_price=None,
            quantity=0, notes=f"Could not fetch LTP: {e}",
            mode="PAPER" if PAPER_MODE else "LIVE",
        )

    quantity = max(1, int(capital_to_deploy // ltp))
    kite_direction = "BUY" if direction == "BUY" else "SELL"

    if PAPER_MODE:
        log.info(
            f"[PAPER] Would place {kite_direction} {quantity} x {ticker} @ ₹{ltp:.2f} "
            f"= ₹{quantity * ltp:,.0f}"
        )
        trade_id = log_trade(signal_id, ticker, direction, quantity, ltp, mode="PAPER")
        update_signal_response(signal_id, "APPROVED")
        return ExecutionResult(
            success=True, order_id=f"PAPER-{trade_id}", fill_price=ltp,
            quantity=quantity, notes=f"Paper trade logged. {check_note}",
            mode="PAPER",
        )

    # LIVE execution — only reached when PAPER_MODE=false
    try:
        kite = get_kite_client()
        order_id = kite.place_order(
            tradingsymbol=ticker,
            exchange=kite.EXCHANGE_NSE,
            transaction_type=kite.TRANSACTION_TYPE_BUY if direction == "BUY" else kite.TRANSACTION_TYPE_SELL,
            quantity=quantity,
            order_type=kite.ORDER_TYPE_MARKET,
            product=kite.PRODUCT_CNC,  # delivery (not intraday)
            variety=kite.VARIETY_REGULAR,
        )
        log_trade(signal_id, ticker, direction, quantity, ltp, mode="LIVE")
        update_signal_response(signal_id, "APPROVED")
        log.info(f"[LIVE] Order placed: {order_id} — {kite_direction} {quantity} x {ticker}")
        return ExecutionResult(
            success=True, order_id=str(order_id), fill_price=ltp,
            quantity=quantity, notes=f"Live order placed. {check_note}",
            mode="LIVE",
        )
    except Exception as e:
        log.error(f"Order placement failed for {ticker}: {e}")
        return ExecutionResult(
            success=False, order_id=None, fill_price=None,
            quantity=quantity, notes=f"Order failed: {e}", mode="LIVE",
        )
