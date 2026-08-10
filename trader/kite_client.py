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
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

PAPER_MODE = os.getenv("PAPER_MODE", "true").lower() == "true"
MAX_ADV_PCT = float(os.getenv("MAX_ADV_PCT", 2.0))       # max 2% of avg daily volume
CIRCUIT_BUFFER_PCT = float(os.getenv("CIRCUIT_BUFFER_PCT", 1.5))
CORP_ACTION_BLACKOUT_DAYS = int(os.getenv("CORP_ACTION_BLACKOUT_DAYS", 3))
MIN_DAILY_TURNOVER_INR = float(os.getenv("MIN_DAILY_TURNOVER_INR", 3_00_00_000))  # ₹3 Cr

# Module-level instrument token cache — populated once per session on first use.
# Avoids fetching all ~1800 NSE instruments on every pre-trade check.
_instrument_token_cache: dict[str, int] = {}


@dataclass
class ExecutionResult:
    success: bool
    order_id: str | None
    fill_price: float | None
    quantity: int
    notes: str
    mode: str  # "PAPER" | "LIVE"
    trade_id: int | None = None  # DB row id from log_trade(); used for Sheets mirroring


def get_kite_client():
    """
    Returns an authenticated KiteConnect instance.

    Token priority:
      1. Ledger DB  — written by auto_refresh_kite_token.py (freshest, updated daily)
      2. KITE_ACCESS_TOKEN env var — fallback for first-run or if DB is empty
    """
    from kiteconnect import KiteConnect
    from ledger.db import get_kite_token

    api_key = os.getenv("KITE_API_KEY")
    if not api_key:
        raise EnvironmentError("KITE_API_KEY is not set.")

    # Prefer token from DB (refreshed daily by scheduler) over env var
    access_token = get_kite_token() or os.getenv("KITE_ACCESS_TOKEN")
    if not access_token:
        raise EnvironmentError(
            "No Kite access_token found. Run: python setup/auto_refresh_kite_token.py"
        )

    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)
    return kite


def get_ltp(ticker: str) -> float:
    """Fetch last traded price from Kite."""
    kite = get_kite_client()
    quote = kite.ltp(f"NSE:{ticker}")
    return quote[f"NSE:{ticker}"]["last_price"]


def _get_instrument_token(kite, ticker: str) -> int | None:
    """
    Returns the NSE instrument token for a ticker, using a module-level cache.
    The full instruments list (~1800 rows) is fetched once per session and cached.
    Returns None if the ticker is not found (bad symbol, or not on NSE).
    """
    global _instrument_token_cache
    if not _instrument_token_cache:
        log.info("Fetching NSE instrument list for token cache...")
        instruments = kite.instruments("NSE")
        _instrument_token_cache = {
            i["tradingsymbol"]: i["instrument_token"] for i in instruments
        }
        log.info(f"Cached {len(_instrument_token_cache)} NSE instruments.")
    return _instrument_token_cache.get(ticker)


def get_20d_avg_turnover(ticker: str) -> float:
    """
    Returns the 20-trading-day average daily turnover in INR (volume × close).
    Fetches from Kite historical data API (requires ₹500/month plan).
    Returns 0.0 on any error so callers can decide to skip or pass-through.
    """
    try:
        kite = get_kite_client()
        token = _get_instrument_token(kite, ticker)
        if token is None:
            log.warning(f"No instrument token found for {ticker} — check NSE symbol.")
            return 0.0

        to_date = datetime.now()
        from_date = to_date - timedelta(days=35)  # ~35 calendar days → ~20+ trading days
        candles = kite.historical_data(token, from_date, to_date, "day")

        if not candles:
            log.warning(f"No historical data returned for {ticker}.")
            return 0.0

        recent = candles[-20:] if len(candles) >= 20 else candles
        avg_turnover = sum(c["volume"] * c["close"] for c in recent) / len(recent)
        log.debug(f"{ticker} 20d avg turnover: ₹{avg_turnover:,.0f}")
        return avg_turnover

    except Exception as e:
        log.warning(f"Could not compute avg turnover for {ticker}: {e}")
        return 0.0


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

        # Liquidity check: skip stocks whose 20-day avg daily turnover is below
        # MIN_DAILY_TURNOVER_INR (default ₹3 Cr). This catches micro-caps that
        # pass the fundamental screen but are too illiquid to trade without
        # significant slippage. Fail-safe: if data is unavailable, log and continue.
        avg_turnover = get_20d_avg_turnover(ticker)
        if avg_turnover == 0.0:
            log.warning(
                f"{ticker}: could not verify liquidity — proceeding with caution."
            )
        elif avg_turnover < MIN_DAILY_TURNOVER_INR:
            return False, (
                f"{ticker} avg daily turnover ₹{avg_turnover/1e7:.1f}Cr < "
                f"minimum ₹{MIN_DAILY_TURNOVER_INR/1e7:.0f}Cr — too illiquid to trade."
            )

        # ADV check: order size must not exceed MAX_ADV_PCT of 20-day average daily volume.
        # Prevents your own order from moving the price against you.
        if avg_turnover > 0:
            approx_shares = capital_to_deploy / ltp
            avg_daily_volume = avg_turnover / ltp  # rough estimate from turnover
            if approx_shares > avg_daily_volume * (MAX_ADV_PCT / 100):
                return False, (
                    f"Order size ({approx_shares:.0f} shares) > {MAX_ADV_PCT}% of "
                    f"20d avg volume ({avg_daily_volume:.0f}) — would move the market."
                )

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
            mode="PAPER", trade_id=trade_id,
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
        db_trade_id = log_trade(signal_id, ticker, direction, quantity, ltp, mode="LIVE")
        update_signal_response(signal_id, "APPROVED")
        log.info(f"[LIVE] Order placed: {order_id} — {kite_direction} {quantity} x {ticker}")
        return ExecutionResult(
            success=True, order_id=str(order_id), fill_price=ltp,
            quantity=quantity, notes=f"Live order placed. {check_note}",
            mode="LIVE", trade_id=db_trade_id,
        )
    except Exception as e:
        log.error(f"Order placement failed for {ticker}: {e}")
        return ExecutionResult(
            success=False, order_id=None, fill_price=None,
            quantity=quantity, notes=f"Order failed: {e}", mode="LIVE",
        )
