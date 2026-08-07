"""
Telegram alert + approve/reject bot.

Locked behaviour — do not change these without revisiting the decision log:
  - Alert window = FULL TRADING DAY (not 5 minutes)
  - No response by market close = NO TRADE, logged as missed opportunity
  - Auto-fire on timeout is explicitly disabled until month 4+ review

Flow:
  1. send_trade_alert() sends a message with Approve / Reject buttons
  2. Telegram calls our webhook when you tap a button
  3. handle_callback() fires — writes your response to the ledger,
     calls execute_trade() if approved, logs missed if no response by EOD

Setup:
  - TELEGRAM_BOT_TOKEN: get from @BotFather
  - TELEGRAM_CHAT_ID: get from @userinfobot (your personal chat ID)
  - Webhook URL: set this after Railway deployment via:
      python setup/set_telegram_webhook.py
"""

import os
import logging
import json
from datetime import datetime
import pytz

log = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BASE_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


def _post(endpoint: str, payload: dict) -> dict:
    import requests
    r = requests.post(f"{BASE_URL}/{endpoint}", json=payload, timeout=10)
    r.raise_for_status()
    return r.json()


def send_plain_message(text: str) -> bool:
    """Send a plain Markdown message (no buttons). Used for alerts and status updates."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram not configured — message not sent.")
        return False
    try:
        _post("sendMessage", {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "Markdown",
        })
        return True
    except Exception as e:
        log.error(f"Failed to send Telegram message: {e}")
        return False


def send_trade_alert(signal_id: int, signal, qc_verdict, sizing) -> bool:
    """
    Sends the trade alert to Telegram with Approve / Reject inline buttons.
    Returns True if message sent successfully.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        raise EnvironmentError(
            "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in .env. "
            "See README for @BotFather and @userinfobot setup steps."
        )

    regime_emoji = {
        "extreme_fear_crash": "🔴", "bear": "🟠", "sideways": "🟡",
        "bull": "🟢", "euphoria": "🚀",
    }.get(signal.regime.value, "⚪")

    direction_emoji = "📈" if signal.direction == "BUY" else "📉"

    text = (
        f"*Trade Signal* {direction_emoji}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"*Ticker:* `{signal.ticker}`\n"
        f"*Direction:* {signal.direction}\n"
        f"*Regime:* {regime_emoji} {signal.regime.value.replace('_', ' ').title()}\n"
        f"*Strategy:* {signal.strategy_bucket}\n"
        f"*Confidence:* {signal.confidence_score:.0f}%\n"
        f"*Capital to deploy:* ₹{sizing.capital_to_deploy:,.0f}\n"
        f"\n"
        f"*Researcher rationale:*\n{signal.rationale[:400]}...\n"
        f"\n"
        f"*QC verdict:* {qc_verdict.verdict}\n"
        f"{qc_verdict.rationale[:300]}\n"
        f"\n"
        f"*Risk Sizer:* {sizing.notes[:200]}\n"
        f"\n"
        f"⏰ _Valid for today's session. No response = no trade._"
    )

    keyboard = {
        "inline_keyboard": [[
            {"text": "✅ Approve", "callback_data": f"approve:{signal_id}"},
            {"text": "❌ Reject",  "callback_data": f"reject:{signal_id}"},
        ]]
    }

    try:
        _post("sendMessage", {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "Markdown",
            "reply_markup": keyboard,
        })
        log.info(f"Alert sent for signal {signal_id} ({signal.ticker})")
        return True
    except Exception as e:
        log.error(f"Failed to send Telegram alert: {e}")
        return False


def handle_callback(callback_data: str, callback_query_id: str):
    """
    Called by the webhook when you tap Approve or Reject.
    callback_data format: "approve:<signal_id>" or "reject:<signal_id>"
    """
    from ledger.db import update_signal_response, skip_signal
    from trader.kite_client import execute_trade
    from ledger.db import get_db

    try:
        action, signal_id_str = callback_data.split(":")
        signal_id = int(signal_id_str)
    except ValueError:
        log.error(f"Malformed callback_data: {callback_data}")
        return

    # Acknowledge the button tap immediately (Telegram requires this within 10s)
    try:
        _post("answerCallbackQuery", {
            "callback_query_id": callback_query_id,
            "text": "Got it ✓" if action == "approve" else "Rejected.",
        })
    except Exception:
        pass  # Don't fail the whole flow if this times out

    if action == "approve":
        # Fetch signal details from ledger and execute
        with get_db() as conn:
            row = conn.execute(
                "SELECT ticker, direction, sized_quantity FROM signals WHERE id=?",
                (signal_id,),
            ).fetchone()

        if not row:
            log.error(f"Signal {signal_id} not found in ledger")
            return

        # Fetch capital_to_deploy from the sizing stored in notes
        # (In the real implementation, store capital_to_deploy as a separate column)
        # TODO: add capital_to_deploy column to signals table
        capital_to_deploy = float(os.getenv("TOTAL_CAPITAL_INR", 1_000_000)) * 0.20  # fallback

        result = execute_trade(
            signal_id=signal_id,
            ticker=row["ticker"],
            direction=row["direction"],
            capital_to_deploy=capital_to_deploy,
        )

        status_text = (
            f"{'[PAPER] ' if result.mode == 'PAPER' else ''}Order {'placed' if result.success else 'failed'}: "
            f"{row['direction']} {result.quantity} x {row['ticker']}"
            + (f" @ ₹{result.fill_price:.2f}" if result.fill_price else "")
            + f"\n{result.notes}"
        )
        _post("sendMessage", {"chat_id": TELEGRAM_CHAT_ID, "text": status_text})

        # Mirror trade open to Google Sheets (non-blocking)
        if result.success and result.trade_id:
            try:
                from sheets.trade_logger import append_trade
                append_trade(
                    trade_id=result.trade_id,
                    signal_id=signal_id,
                    ticker=row["ticker"],
                    direction=row["direction"],
                    quantity=result.quantity,
                    entry_price=result.fill_price or 0.0,
                    entry_time=datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
                    mode=result.mode,
                )
            except Exception as e:
                log.warning(f"Sheets trade log failed (non-critical): {e}")

    elif action == "reject":
        update_signal_response(signal_id, "REJECTED")
        log.info(f"Signal {signal_id} rejected by user.")


def send_eod_missed_opportunities():
    """
    Called at EOD for any signals still in PENDING status (no response received).
    Marks them as NO_RESPONSE and notifies you.
    """
    from ledger.db import get_db, update_signal_response

    with get_db() as conn:
        pending = conn.execute(
            "SELECT id, ticker, direction FROM signals WHERE status='PENDING' AND DATE(created_at)=DATE('now')"
        ).fetchall()

    if not pending:
        return

    for row in pending:
        update_signal_response(row["id"], "NO_RESPONSE")

    missed_text = "📋 *Missed today (no response):*\n" + "\n".join(
        f"  • {r['direction']} {r['ticker']} (signal #{r['id']})" for r in pending
    )
    try:
        _post("sendMessage", {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": missed_text,
            "parse_mode": "Markdown",
        })
    except Exception as e:
        log.error(f"Failed to send missed opportunities summary: {e}")
