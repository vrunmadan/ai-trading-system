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
import requests as _requests
from datetime import datetime

import pytz

log = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

ALERT_EMAIL     = os.getenv("ALERT_EMAIL", "")
RAILWAY_URL     = os.getenv("RAILWAY_URL", "http://localhost:8080").rstrip("/")
APPROVAL_SECRET = os.getenv("APPROVAL_SECRET", "change-me-in-dotenv")

# Resend HTTP API — works on Railway (port 443). No SMTP needed.
# Sign up free at resend.com, copy your API key, set RESEND_API_KEY in Railway.
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")

# Resend requires a verified sender address OR allows "onboarding@resend.dev"
# for testing. Once you add/verify your own domain, change RESEND_FROM below.
# For now: emails are sent FROM onboarding@resend.dev and TO your ALERT_EMAIL.
RESEND_FROM = os.getenv("RESEND_FROM", "AI Trading System <onboarding@resend.dev>")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _configured() -> bool:
    if not RESEND_API_KEY or not ALERT_EMAIL:
        log.warning(
            "Email not configured — set RESEND_API_KEY and ALERT_EMAIL in Railway vars. "
            "Get a free Resend key at resend.com."
        )
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
    """
    Low-level send via Resend HTTP API (port 443 — not blocked by Railway).
    Returns True on success.
    """
    if not _configured():
        return False
    try:
        payload = {
            "from":    RESEND_FROM,
            "to":      [ALERT_EMAIL],
            "subject": subject,
            "html":    html_body,
        }
        if plain_body:
            payload["text"] = plain_body

        resp = _requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {RESEND_API_KEY}",
                "Content-Type":  "application/json",
            },
            json=payload,
            timeout=15,
        )
        if resp.status_code in (200, 201):
            log.info(f"Resend: email sent OK (id={resp.json().get('id', '?')})")
            return True
        else:
            log.error(f"Resend API error {resp.status_code}: {resp.text}")
            return False
    except Exception as e:
        log.error(f"Resend send failed: {e}")
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

    Approve flow:
      Tap "Approve → Open Kite" → our server redirects to Kite basket URL →
      Kite order screen opens pre-filled → user taps Place Order in Kite.

    Returns True if sent successfully.
    """
    if not _configured():
        raise EnvironmentError(
            "RESEND_API_KEY and ALERT_EMAIL must be set in Railway env vars."
        )

    regime_emoji = {
        "extreme_fear_crash": "🔴", "bear": "🟠", "sideways": "🟡",
        "bull": "🟢", "euphoria": "🚀",
    }.get(signal.regime.value, "⚪")

    direction_emoji = "📈" if signal.direction == "BUY" else "📉"
    approve_url = _action_url("approve", signal_id)
    reject_url  = _action_url("reject",  signal_id)

    # Build the Kite basket URL for the fallback link in the email.
    # The approve_url (our webhook) also redirects to this same URL when tapped.
    kite_basket_url = _build_kite_basket_url(
        signal.ticker,
        getattr(signal, "exchange", "NSE"),
        signal.direction,
        sizing.quantity,
    )

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
          <td style="padding:6px 0;font-weight:600">{signal.ticker} ({getattr(signal, "exchange", "NSE")})</td></tr>
      <tr><td style="padding:6px 0;color:#888">Direction</td>
          <td style="padding:6px 0;font-weight:600">{signal.direction}</td></tr>
      <tr><td style="padding:6px 0;color:#888">Quantity</td>
          <td style="padding:6px 0;font-weight:600">{sizing.quantity} shares</td></tr>
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
      <span style="color:#444">{signal.rationale[:400]}</span>
    </div>

    <div style="background:#f8f8f8;border-radius:8px;padding:14px;margin-bottom:24px;font-size:13px">
      <strong>QC verdict: {qc_verdict.verdict}</strong><br>
      <span style="color:#444">{qc_verdict.rationale[:300]}</span><br><br>
      <strong>Sizer notes:</strong> {sizing.notes[:200]}
    </div>

    <!-- Primary action: Approve opens Kite basket via our webhook redirect -->
    <div style="text-align:center;margin-bottom:12px">
      <a href="{approve_url}"
         style="display:inline-block;background:#22c55e;color:#fff;text-decoration:none;
                padding:16px 40px;border-radius:8px;font-size:17px;font-weight:700;
                margin:0 8px 12px">
        ✅&nbsp; Approve → Open Kite
      </a>
      <a href="{reject_url}"
         style="display:inline-block;background:#ef4444;color:#fff;text-decoration:none;
                padding:16px 32px;border-radius:8px;font-size:17px;font-weight:700;
                margin:0 8px 12px">
        ❌&nbsp; Reject
      </a>
    </div>

    <!-- Fallback: direct Kite basket link if the redirect fails -->
    <p style="text-align:center;color:#888;font-size:12px;margin:0 0 16px">
      If Kite doesn't open automatically after Approve:<br>
      <a href="{kite_basket_url}" style="color:#387ed1">Open Kite order directly →</a>
    </p>

    <p style="text-align:center;color:#aaa;font-size:11px;margin:0">
      ⏰ Valid for today's session only. No response = no trade.
    </p>
  </div>
</body>
</html>
"""

    plain = (
        f"Trade Signal #{signal_id} — {signal.ticker} ({getattr(signal, 'exchange', 'NSE')}) {signal.direction}\n"
        f"Quantity: {sizing.quantity} shares  |  Capital: ₹{sizing.capital_to_deploy:,.0f}\n"
        f"Confidence: {signal.confidence_score:.0f}%  |  Regime: {signal.regime.value}\n\n"
        f"Rationale: {signal.rationale[:400]}\n\n"
        f"QC: {qc_verdict.verdict} — {qc_verdict.rationale[:300]}\n\n"
        f"APPROVE (opens Kite): {approve_url}\n"
        f"REJECT:               {reject_url}\n\n"
        f"Direct Kite basket (fallback): {kite_basket_url}\n\n"
        f"Valid for today's session. No response = no trade."
    )

    sent = _send_email(subject, html, plain_body=plain)
    if sent:
        log.info(f"Gmail alert sent for signal #{signal_id} ({signal.ticker})")
    return sent


def _build_kite_basket_url(ticker: str, exchange: str, direction: str, quantity: int) -> str:
    """
    Constructs a Kite basket order URL. When the user opens this URL in their browser,
    Kite's order screen opens with the trade pre-filled — they just tap Place Order.

    URL format:  https://kite.zerodha.com/connect/basket?api_key=<key>&data=<json>
    Data schema: list of order dicts (Kite accepts up to 20 legs).
    """
    import json
    import urllib.parse

    api_key = os.getenv("KITE_API_KEY", "")
    order = [{
        "variety": "regular",
        "tradingsymbol": ticker,
        "exchange": exchange,
        "transaction_type": direction,   # "BUY" or "SELL"
        "order_type": "MARKET",
        "quantity": max(1, int(quantity)),
        "readonly": False,               # user can still edit before placing
    }]
    encoded = urllib.parse.quote(json.dumps(order))
    return f"https://kite.zerodha.com/connect/basket?api_key={api_key}&data={encoded}"


def handle_email_action(action: str, signal_id: int):
    """
    Called by webhook_server.py when the user taps Approve or Reject.

    Returns a 3-tuple: (message, success, kite_basket_url_or_None)

    For 'approve': kite_basket_url is set — the webhook server redirects the user's
    browser there so Kite opens with the trade pre-filled. The user taps Place Order
    once in Kite's own interface. We never call the trading API automatically.

    For 'reject': kite_basket_url is None — just show a confirmation HTML page.
    """
    from ledger.db import update_signal_response, get_db

    if action == "approve":
        with get_db() as conn:
            row = conn.execute(
                "SELECT ticker, exchange, direction, sized_quantity, capital_to_deploy "
                "FROM signals WHERE id=?",
                (signal_id,),
            ).fetchone()

        if not row:
            log.error(f"Signal {signal_id} not found in ledger")
            return "Signal not found.", False, None

        ticker   = row["ticker"]
        exchange = row["exchange"] if row["exchange"] else "NSE"
        direction = row["direction"]

        # Prefer the Risk Sizer's pre-computed quantity; fall back to capital/notional estimate
        quantity = row["sized_quantity"] or 1

        # Mark as APPROVED in the ledger immediately — user has confirmed intent.
        # Actual execution is in Kite's hands from here. Position monitor will
        # reconcile if the trade doesn't go through.
        update_signal_response(signal_id, "APPROVED")
        log.info(f"Signal #{signal_id} approved — launching Kite basket for {direction} {quantity}×{exchange}:{ticker}")

        basket_url = _build_kite_basket_url(ticker, exchange, direction, quantity)

        send_plain_email(
            subject=f"✅ Kite basket launched — {ticker} {direction} #{signal_id}",
            body=(
                f"You approved signal #{signal_id}.\n"
                f"Kite basket opened: {direction} {quantity} × {exchange}:{ticker}\n\n"
                f"If the Kite screen did not open automatically, use this link:\n{basket_url}"
            ),
        )

        return (
            f"Kite basket launched: {direction} {quantity} × {exchange}:{ticker}. "
            "Tap Place Order in Kite to execute.",
            True,
            basket_url,
        )

    elif action == "reject":
        update_signal_response(signal_id, "REJECTED")
        log.info(f"Signal #{signal_id} rejected by user.")
        send_plain_email(
            subject=f"❌ Signal #{signal_id} rejected",
            body=f"Signal #{signal_id} marked as REJECTED.",
        )
        return f"Signal #{signal_id} rejected.", True, None

    else:
        return "Unknown action.", False, None


def send_kite_login_email() -> bool:
    """
    Sent at 7:30 AM IST every weekday. One big button — user taps it,
    logs into Zerodha normally, and Railway captures the token automatically.
    No passwords, no TOTP secrets stored anywhere.

    Requires the Kite app redirect URL to be set to:
      https://ai-trading-system-production-6af9.up.railway.app/kite_callback
    at https://developers.kite.trade/apps
    """
    api_key    = os.getenv("KITE_API_KEY", "")
    railway_url = RAILWAY_URL

    login_url = f"https://kite.zerodha.com/connect/login?v=3&api_key={api_key}"

    html = f"""
<!DOCTYPE html>
<html>
<head><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
             max-width:480px;margin:0 auto;padding:32px 20px;background:#f5f5f5">
  <div style="background:#fff;border-radius:12px;padding:32px 28px;
              box-shadow:0 2px 8px rgba(0,0,0,.08);text-align:center">

    <div style="font-size:48px;margin-bottom:12px">🔑</div>
    <h2 style="margin:0 0 8px;font-size:22px">Activate today's trading</h2>
    <p style="color:#666;margin:0 0 28px;font-size:14px;line-height:1.5">
      Tap the button below and log into Zerodha.<br>
      Railway captures the token automatically — you're done in 20 seconds.
    </p>

    <a href="{login_url}"
       style="display:block;background:#387ed1;color:#fff;text-decoration:none;
              padding:16px 0;border-radius:10px;font-size:18px;font-weight:700;
              letter-spacing:0.3px;margin-bottom:24px">
      Log in to Zerodha →
    </a>

    <p style="color:#aaa;font-size:12px;margin:0;line-height:1.6">
      After login you'll see a green "Ready to trade" confirmation.<br>
      If you don't activate by 9:15 AM, the system will skip today's cycles.
    </p>
  </div>
</body>
</html>"""

    plain = (
        f"Tap to activate today's Kite trading session:\n{login_url}\n\n"
        "After logging in you'll see a green confirmation page. Done.\n"
        "If not activated by 9:15 AM, today's research cycles will be skipped."
    )

    sent = _send_email("🔑 Activate today's trading — tap to log in", html, plain_body=plain)
    if sent:
        log.info("Kite morning login email sent.")
    return sent


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


def send_daily_cycle_summary() -> None:
    """
    Sent at 15:35 IST alongside the EOD sweep.
    Shows a plain-English summary of today's research cycles regardless of
    whether any signal fired. This gives visibility into WHY no recommendations
    came through — was it regime, threshold, or errors?

    Reads from the DB signals table (today's records) and from a module-level
    cycle log updated by the scheduler.
    """
    import datetime
    import pytz
    from ledger.db import get_db

    IST = pytz.timezone("Asia/Kolkata")
    today_ist = datetime.datetime.now(IST).strftime("%Y-%m-%d")

    with get_db() as conn:
        signals_today = conn.execute(
            "SELECT id, ticker, exchange, direction, confidence_score, status, created_at "
            "FROM signals WHERE DATE(created_at) = ? ORDER BY created_at",
            (today_ist,)
        ).fetchall()

    # Pull today's cycle log for best scores
    try:
        from ledger.db import get_cycle_log
        cycle_rows = get_cycle_log(days=1)
        scored = [r for r in cycle_rows if r.get("confidence_score") is not None]
        if scored:
            top5 = sorted(scored, key=lambda r: -(r["confidence_score"] or 0))[:5]
            top_lines = "\n".join(
                f"  {r['ticker']:12s} {r['strategy']:20s}  "
                f"tech={r['technical_score'] or 0:.0f}  fund={r['fundamental_score'] or 0:.0f}  "
                f"conf={r['confidence_score']:.0f}%  [{r['verdict']}]"
                for r in top5
            )
            top_block = f"\nTop 5 scores today (75% needed to fire):\n{top_lines}"
        else:
            top_block = ""
    except Exception:
        top_block = ""

    if signals_today:
        signal_lines = "\n".join(
            f"  • {r['direction']} {r['ticker']} ({r['exchange']}) — "
            f"{r['confidence_score']:.0f}% confidence — {r['status']}"
            for r in signals_today
        )
        signal_block = f"Signals generated today:\n{signal_lines}"
    else:
        signal_block = "No signals reached the confidence threshold today (75%+ required)."

    body = (
        f"Daily research cycle summary — {today_ist}\n"
        f"{'=' * 50}\n\n"
        f"{signal_block}"
        f"{top_block}\n\n"
        f"Full detail:\n"
        f"  {RAILWAY_URL}/cycle_history?secret=<APPROVAL_SECRET>&days=1\n"
    )

    send_plain_email(
        subject=f"📊 Daily summary — {len(signals_today)} signal{'s' if len(signals_today) != 1 else ''} today",
        body=body,
    )
