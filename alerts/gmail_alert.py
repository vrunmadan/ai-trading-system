"""
Gmail alert + approve/reject flow.

Replaces telegram_bot.py — same contract, different transport.

Flow:
  1. send_trade_alert() emails you a signal with two big links:
       https://your-app.railway.app/email_action?action=approve&id=<n>&token=<hmac>
       https://your-app.railway.app/email_action?action=reject&id=<n>&token=<hmac>
  2. You tap one from your phone. Browser GETs the link.
  3. webhook_server.py verifies the HMAC token and calls handle_email_action().
  4. handle_email_action() fires execute_trade() or logs rejection.

Required .env keys:
  GMAIL_APP_PASSWORD   — Google App Password (not your account password).
                         Enable at: myaccount.google.com/apppasswords
                         (Requires 2-Step Verification to be on.)
  ALERT_EMAIL          — Your Gmail address (both sender and recipient).
  RAILWAY_URL          — Your Railway deployment URL, e.g. https://my-app.railway.app
  APPROVAL_SECRET      — Random string used to sign approve/reject tokens.
                         Generate with: python -c "import secrets; print(secrets.token_hex(32))"

Locked behaviour (same as Telegram version):
  - Alert window = FULL TRADING DAY
  - No response by market close = NO TRADE, logged as missed opportunity
  - Auto-fire on timeout is explicitly disabled until month 4+ review
"""

import hashlib
import hmac
import logging
import os
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pytz

log = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
ALERT_EMAIL        = os.getenv("ALERT_EMAIL", "")
RAILWAY_URL        = os.getenv("RAILWAY_URL", "http://localhost:8080").rstrip("/")
APPROVAL_SECRET    = os.getenv("APPROVAL_SECRET", "change-me-in-dotenv")

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _configured() -> bool:
    if not GMAIL_APP_PASSWORD or not ALERT_EMAIL:
        log.warning("Gmail not configured — set GMAIL_APP_PASSWORD and ALERT_EMAIL in .env.")
        return False
    return True


def _make_token(action: str, signal_id: int) -> str:
    """HMAC-SHA256 token so approve/reject links can't be forged."""
    msg = f"{action}:{signal_id}".encode()
    return hmac.new(APPROVAL_SECRET.encode(), msg, hashlib.sha256).hexdigest()


def verify_token(action: str, signal_id: int, token: str) -> bool:
    expected = _make_token(action, signal_id)
    return hmac.compare_digest(expected, token)


def _action_url(action: str, signal_id: int) -> str:
    token = _make_token(action, signal_id)
    return f"{RAILWAY_URL}/email_action?action={action}&id={signal_id}&token={token}"


def _send_email(subject: str, html_body: str, plain_body: str = "") -> bool:
    """Low-level send. Returns True on success."""
    if not _configured():
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = ALERT_EMAIL
        msg["To"]      = ALERT_EMAIL

        if plain_body:
            msg.attach(MIMEText(plain_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(ALERT_EMAIL, GMAIL_APP_PASSWORD)
            smtp.sendmail(ALERT_EMAIL, ALERT_EMAIL, msg.as_string())
        return True
    except Exception as e:
        log.error(f"Gmail send failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Public API — mirrors telegram_bot.py interface
# ---------------------------------------------------------------------------

def send_plain_email(subject: str, body: str) -> bool:
    """
    Send a plain-text notification email. Used for alerts and status updates.
    Replaces send_plain_message() from telegram_bot.
    """
    html = f"<pre style='font-family:monospace;font-size:14px'>{body}</pre>"
    return _send_email(subject, html, plain_body=body)


def send_trade_alert(signal_id: int, signal, qc_verdict, sizing) -> bool:
    """
    Email the trade signal with Approve / Reject links.
    Returns True if sent successfully.
    """
    if not _configured():
        raise EnvironmentError(
            "GMAIL_APP_PASSWORD and ALERT_EMAIL must be set in .env. "
            "See README for Gmail App Password setup steps."
        )

    regime_emoji = {
        "extreme_fear_crash": "🔴", "bear": "🟠", "sideways": "🟡",
        "bull": "🟢", "euphoria": "🚀",
    }.get(signal.regime.value, "⚪")

    direction_emoji = "📈" if signal.direction == "BUY" else "📉"
    approve_url = _action_url("approve", signal_id)
    reject_url  = _action_url("reject",  signal_id)

    subject = f"{direction_emoji} Trade Signal #{signal_id} — {signal.ticker} {signal.direction}"

    html = f"""
<!DOCTYPE html>
<html>
<head><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
             max-width:560px;margin:0 auto;padding:24px;background:#f5f5f5">

  <div style="background:#fff;border-radius:12px;padding:28px;box-shadow:0 2px 8px rgba(0,0,0,.08)">

    <h2 style="margin:0 0 4px;font-size:20px">
      {direction_emoji}&nbsp; Trade Signal &nbsp;<code style="font-size:16px">#{signal_id}</code>
    </h2>
    <p style="margin:0 0 20px;color:#666;font-size:13px">
      {datetime.now(IST).strftime("%d %b %Y, %I:%M %p IST")}
    </p>

    <table style="width:100%;border-collapse:collapse;font-size:14px;margin-bottom:20px">
      <tr><td style="padding:6px 0;color:#888;width:40%">Ticker</td>
          <td style="padding:6px 0;font-weight:600">{signal.ticker}</td></tr>
      <tr><td style="padding:6px 0;color:#888">Direction</td>
          <td style="padding:6px 0;font-weight:600">{signal.direction}</td></tr>
      <tr><td style="padding:6px 0;color:#888">Regime</td>
          <td style="padding:6px 0">{regime_emoji} {signal.regime.value.replace('_', ' ').title()}</td></tr>
      <tr><td style="padding:6px 0;color:#888">Strategy</td>
          <td style="padding:6px 0">{signal.strategy_bucket}</td></tr>
      <tr><td style="padding:6px 0;color:#888">Confidence</td>
          <td style="padding:6px 0">{signal.confidence_score:.0f}%</td></tr>
      <tr><td style="padding:6px 0;color:#888">Capital</td>
          <td style="padding:6px 0">₹{sizing.capital_to_deploy:,.0f}</td></tr>
    </table>

    <div style="background:#f8f8f8;border-radius:8px;padding:14px;margin-bottom:16px;font-size:13px">
      <strong>Researcher rationale</strong><br>
      <span style="color:#444">{signal.rationale[:400]}...</span>
    </div>

    <div style="background:#f8f8f8;border-radius:8px;padding:14px;margin-bottom:24px;font-size:13px">
      <strong>QC verdict: {qc_verdict.verdict}</strong><br>
      <span style="color:#444">{qc_verdict.rationale[:300]}</span><br><br>
      <strong>Sizer notes:</strong> {sizing.notes[:200]}
    </div>

    <div style="text-align:center;margin-bottom:20px">
      <a href="{approve_url}"
         style="display:inline-block;background:#22c55e;color:#fff;text-decoration:none;
                padding:14px 36px;border-radius:8px;font-size:16px;font-weight:600;
                margin:0 8px">
        ✅&nbsp; Approve
      </a>
      <a href="{reject_url}"
         style="display:inline-block;background:#ef4444;color:#fff;text-decoration:none;
                padding:14px 36px;border-radius:8px;font-size:16px;font-weight:600;
                margin:0 8px">
        ❌&nbsp; Reject
      </a>
    </div>

    <p style="text-align:center;color:#aaa;font-size:12px;margin:0">
      ⏰ Valid for today's session. No response = no trade.
    </p>
  </div>
</body>
</html>
"""

    plain = (
        f"Trade Signal #{signal_id} — {signal.ticker} {signal.direction}\n"
        f"Confidence: {signal.confidence_score:.0f}%  |  Capital: ₹{sizing.capital_to_deploy:,.0f}\n"
        f"Regime: {signal.regime.value}\n\n"
        f"Rationale: {signal.rationale[:400]}\n\n"
        f"QC: {qc_verdict.verdict} — {qc_verdict.rationale[:300]}\n\n"
        f"APPROVE: {approve_url}\n"
        f"REJECT:  {reject_url}\n\n"
        f"Valid for today's session. No response = no trade."
    )

    sent = _send_email(subject, html, plain_body=plain)
    if sent:
        log.info(f"Gmail alert sent for signal #{signal_id} ({signal.ticker})")
    return sent


def handle_email_action(action: str, signal_id: int):
    """
    Called by webhook_server.py when the user taps Approve or Reject.
    Mirrors handle_callback() from telegram_bot.
    """
    from ledger.db import update_signal_response, get_db

    if action == "approve":
        from trader.kite_client import execute_trade

        with get_db() as conn:
            row = conn.execute(
                "SELECT ticker, direction, sized_quantity FROM signals WHERE id=?",
                (signal_id,),
            ).fetchone()

        if not row:
            log.error(f"Signal {signal_id} not found in ledger")
            return "Signal not found.", False

        capital_to_deploy = float(os.getenv("TOTAL_CAPITAL_INR", 1_000_000)) * 0.20

        result = execute_trade(
            signal_id=signal_id,
            ticker=row["ticker"],
            direction=row["direction"],
            capital_to_deploy=capital_to_deploy,
        )

        status = (
            f"{'[PAPER] ' if result.mode == 'PAPER' else ''}"
            f"Order {'placed' if result.success else 'FAILED'}: "
            f"{row['direction']} {result.quantity} × {row['ticker']}"
            + (f" @ ₹{result.fill_price:.2f}" if result.fill_price else "")
        )
        log.info(status)

        send_plain_email(
            subject=f"✅ Trade executed — {row['ticker']} #{signal_id}",
            body=status + "\n\n" + result.notes,
        )

        # Mirror to Sheets
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

        return status, result.success

    elif action == "reject":
        update_signal_response(signal_id, "REJECTED")
        log.info(f"Signal #{signal_id} rejected by user.")
        send_plain_email(
            subject=f"❌ Signal #{signal_id} rejected",
            body=f"Signal #{signal_id} marked as REJECTED.",
        )
        return f"Signal #{signal_id} rejected.", True

    else:
        return "Unknown action.", False


def send_eod_missed_opportunities() -> None:
    """
    Called at EOD for any signals still in PENDING status.
    Marks them as NO_RESPONSE and emails a summary.
    """
    from ledger.db import get_db, update_signal_response

    with get_db() as conn:
        pending = conn.execute(
            "SELECT id, ticker, direction FROM signals "
            "WHERE status='PENDING' AND DATE(created_at)=DATE('now')"
        ).fetchall()

    if not pending:
        return

    for row in pending:
        update_signal_response(row["id"], "NO_RESPONSE")

    lines = "\n".join(
        f"  • {r['direction']} {r['ticker']} (signal #{r['id']})" for r in pending
    )
    send_plain_email(
        subject=f"📋 Missed today ({len(pending)} signal{'s' if len(pending) > 1 else ''})",
        body=f"These signals received no response today:\n\n{lines}\n\nAll marked NO_RESPONSE.",
    )
