"""
Gmail alert + approve/reject flow.

Flow:
  1. send_trade_alert() emails you a signal with two big links:
       https://your-app.railway.app/email_action?action=approve&id=<n>&token=<hmac>
       https://your-app.railway.app/email_action?action=reject&id=<n>&token=<hmac>
  2. You tap one from your phone. Browser GETs the link.
  3. webhook_server.py verifies the HMAC token and calls handle_email_action().
  4. On approve, handle_email_action() marks the signal APPROVED and returns a
     Kite Connect basket-order URL — the caller (webhook_server.py) redirects
     your browser there, and you place the order yourself inside Kite's own
     UI. No server-side execute_trade() call happens on this path.
  5. On reject, it marks the signal REJECTED and logs it.

Required .env keys:
  GMAIL_APP_PASSWORD   — Google App Password (not your account password).
                         Enable at: myaccount.google.com/apppasswords
                         (Requires 2-Step Verification to be on.)
  ALERT_EMAIL          — Your Gmail address (both sender and recipient).
  RAILWAY_URL          — Your Railway deployment URL, e.g. https://my-app.railway.app
  APPROVAL_SECRET      — Random string used to sign approve/reject tokens.
                         Generate with: python -c "import secrets; print(secrets.token_hex(32))"

Locked behaviour:
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
APPROVAL_SECRET = os.getenv("APPROVAL_SECRET", "")

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
    if not APPROVAL_SECRET:
        log.error("APPROVAL_SECRET is not set — refusing to verify any approve/reject token.")
        return False
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
# Public API
# ---------------------------------------------------------------------------

def send_plain_email(subject: str, body: str) -> bool:
    """
    Send a plain-text notification email. Used for alerts and status updates.
    Rendered as monospace <pre> HTML, so whitespace is preserved exactly as
    written — but this means it is NOT a Markdown renderer. Callers must not
    pass Telegram/Markdown-style formatting (*bold*, _italic_) expecting it
    to render; it will show up as literal asterisks/underscores.
    """
    html = f"<pre style='font-family:monospace;font-size:14px'>{body}</pre>"
    return _send_email(subject, html, plain_body=body)


def send_trade_alert(signal_id: int, signal, qc_verdict, sizing, risk_flags=None) -> bool:
    """
    Email the trade signal with Approve / Reject links.

    Approve flow:
      Tap "Approve → Open Kite" → our server redirects to Kite basket URL →
      Kite order screen opens pre-filled → user taps Place Order in Kite.

    risk_flags: optional list of advisory risk warnings (exposure / sector /
      position count / sizer limits). When present they are shown as an
      advisory banner — the trade is NOT blocked, the decision is the user's.

    Returns True if sent successfully.
    """
    if not _configured():
        raise EnvironmentError(
            "RESEND_API_KEY and ALERT_EMAIL must be set in Railway env vars."
        )

    risk_flags = risk_flags or []
    if risk_flags:
        _flag_items = "".join(f"<li style='margin:2px 0'>{f}</li>" for f in risk_flags)
        risk_html = (
            "<div style=\"background:#fff7ed;border:1px solid #fdba74;border-radius:8px;"
            "padding:14px;margin-bottom:16px;font-size:13px\">"
            "<strong>⚠ Risk flags (advisory — your call)</strong>"
            "<div style='color:#9a3412;margin-top:4px'>These limits are informational. "
            "The signal was NOT blocked; you decide whether to trade.</div>"
            f"<ul style='margin:8px 0 0;padding-left:18px;color:#7c2d12'>{_flag_items}</ul>"
            "</div>"
        )
        risk_plain = (
            "\n⚠ RISK FLAGS (advisory — your call; the signal was NOT blocked):\n"
            + "".join(f"  - {f}\n" for f in risk_flags)
        )
    else:
        risk_html = ""
        risk_plain = ""

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

    {risk_html}

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
        f"QC: {qc_verdict.verdict} — {qc_verdict.rationale[:300]}\n"
        f"{risk_plain}\n"
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


def _current_mode() -> str:
    """PAPER or LIVE, read at call time so a config change does not need a restart."""
    return "PAPER" if os.getenv("PAPER_MODE", "true").lower() == "true" else "LIVE"


# Statuses from which an approval may legitimately proceed.
#
#   PENDING       the normal case: alerted, awaiting your answer
#   APPROVED      a re-tap of the same link; the live-trade guard below turns
#                 this into "re-opening the same basket", not a second position
#   NOT_EXECUTED  the reconciler established the order never reached the
#                 market, so a deliberate retry is legitimate
#
# Everything else is a signal the pipeline already decided against, and
# approving it would override that decision from outside. QC_BLOCKED is the
# one that matters most: QC adversarially reviewed the trade and returned
# DISAGREE. Until this check existed the QC gate was a property of one code
# path in run_cycle rather than of the data, so any signal carrying a share
# quantity could be approved regardless of what the pipeline concluded.
_APPROVABLE_STATUSES = {"PENDING", "APPROVED", "NOT_EXECUTED"}

_STATUS_REFUSAL = {
    "QC_BLOCKED": "the QC fact-checker reviewed this trade and rejected it",
    "QC_ERROR": "the QC fact-checker could not be reached, so this trade was never validated",
    "REJECTED": "you rejected this signal",
    "NO_RESPONSE": "this signal went unanswered and was swept at end of day",
    "SKIPPED": "this signal was dropped before an alert was sent",
    "EXECUTED": "this signal already has a confirmed fill",
    "DROPPED_SIZER": "the Risk Sizer rejected this position",
    "DROPPED_NO_PRICE": "no live price could be fetched, so it was never sized",
    "DROPPED_SUBMIN": "the position came out below the minimum tradeable size",
}


def handle_email_action(action: str, signal_id: int):
    """
    Called by webhook_server.py when the user taps Approve or Reject.

    Returns a 3-tuple: (message, success, kite_basket_url_or_None)

    For 'approve': kite_basket_url is set — the webhook server redirects the user's
    browser there so Kite opens with the trade pre-filled. The user taps Place Order
    once in Kite's own interface. We never call the trading API automatically.

    For 'reject': kite_basket_url is None — just show a confirmation HTML page.
    """
    from ledger.db import (
        get_db,
        get_live_trade_for_signal,
        log_pending_trade,
        update_signal_response,
    )

    if action == "approve":
        with get_db() as conn:
            row = conn.execute(
                "SELECT ticker, exchange, direction, sized_quantity, "
                "capital_to_deploy, status "
                "FROM signals WHERE id=?",
                (signal_id,),
            ).fetchone()

        if not row:
            log.error(f"Signal {signal_id} not found in ledger")
            return "Signal not found.", False, None

        ticker   = row["ticker"]
        exchange = row["exchange"] if row["exchange"] else "NSE"
        direction = row["direction"]

        # Enforce the pipeline's own verdict before anything else. Without
        # this, approval was gated on quantity alone, so a signal QC had
        # explicitly refused would still open a pre-filled Kite basket.
        sig_status = (row["status"] or "").upper()
        if sig_status not in _APPROVABLE_STATUSES:
            reason = _STATUS_REFUSAL.get(
                sig_status, f"this signal is in state {sig_status}"
            )
            log.warning(
                f"Refusing to approve signal {signal_id}: status={sig_status!r} "
                f"is not approvable."
            )
            return (
                f"This signal cannot be approved because {reason} "
                f"(status: {sig_status}). No Kite basket was opened and the "
                f"signal was left unchanged.",
                False,
                None,
            )

        # The quantity is computed in run_cycle Step 3b from the live LTP and
        # persisted with the signal. There is deliberately no fallback: a
        # missing or zero quantity means the sizing chain did not complete,
        # and silently substituting a number would send a wrong-sized order.
        quantity = row["sized_quantity"]
        if not quantity or int(quantity) < 1:
            log.error(
                f"Signal {signal_id} has no usable quantity "
                f"(sized_quantity={quantity!r}) — refusing to build a Kite basket."
            )
            return (
                "This signal has no valid order quantity, so no Kite basket was "
                "opened. The signal was left untouched. This means the sizing step "
                "did not complete when the alert was generated — check the cycle "
                "logs for that signal.",
                False,
                None,
            )
        quantity = int(quantity)

        # Double-approve guard: a link is valid indefinitely, so a second tap
        # would otherwise write a second trade row and double the recorded
        # exposure for a single order.
        existing = get_live_trade_for_signal(signal_id)
        if existing:
            log.info(
                f"Signal #{signal_id} already has trade #{existing['id']} "
                f"({existing.get('fill_status')}) — re-opening basket without "
                f"writing a second row."
            )
            basket_url = _build_kite_basket_url(ticker, exchange, direction, quantity)
            return (
                f"This signal was already approved (trade #{existing['id']}, "
                f"{existing.get('fill_status')}). Re-opening the same Kite basket; "
                f"no second position was recorded.",
                True,
                basket_url,
            )

        # Record the approved intent BEFORE touching the signal, so a ledger
        # failure leaves both untouched rather than marking a signal APPROVED
        # with no matching position. entry_price is the expected price; the EOD
        # reconciler replaces it with the real average from Kite.
        expected_price = float(row["capital_to_deploy"] or 0.0) / quantity
        try:
            trade_id = log_pending_trade(
                signal_id=signal_id,
                ticker=ticker,
                exchange=exchange,
                direction=direction,
                quantity=quantity,
                expected_price=expected_price,
                mode=_current_mode(),
            )
        except Exception as e:
            log.error(
                f"Could not write PENDING trade for signal {signal_id}: {e}",
                exc_info=True,
            )
            return (
                "Could not record this trade in the ledger, so no Kite basket was "
                "opened and the signal was left untouched. Approving without a "
                "ledger row would leave the risk gates blind to this position.",
                False,
                None,
            )

        # Intent is recorded; now mark the signal and hand off to Kite.
        update_signal_response(signal_id, "APPROVED")
        log.info(
            f"Signal #{signal_id} approved — trade #{trade_id} recorded PENDING "
            f"({direction} {quantity}×{exchange}:{ticker} @ ~₹{expected_price:,.2f}); "
            f"launching Kite basket."
        )

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
            f"Kite basket launched: {direction} {quantity} × {exchange}:{ticker} "
            f"(trade #{trade_id}, pending fill confirmation). "
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


def send_qc_unreachable_alert(signal_id, signal, sizing, qc_verdict, streak: int) -> bool:
    """
    Fired the moment a real, sized candidate is dropped because QC could not
    be reached.

    Deliberately immediate rather than batched into the EOD summary: a blocked
    trade is only actionable while the market is open, and the point of this
    alert is that the user finds out in time to do something about it.

    No Approve/Reject links. The thesis was never validated, so there is
    nothing safe to approve. This is a notification, not a decision.
    """
    ticker = signal.ticker
    streak_line = (
        f"This is failure #{streak} in a row."
        if streak > 1 else "This is the first failure in the current run."
    )
    body = (
        "A trade signal cleared the Researcher AND the Risk Sizer, then was "
        "blocked because the QC fact-checker could not be reached.\n\n"
        "This is NOT a QC rejection. QC never answered.\n"
        + "=" * 52 + "\n\n"
        f"  Ticker      {ticker} ({getattr(signal, 'exchange', 'NSE')})\n"
        f"  Direction   {signal.direction}\n"
        f"  Strategy    {signal.strategy_bucket}\n"
        f"  Regime      {signal.regime.value}\n"
        f"  Confidence  {signal.confidence_score:.0f}%\n"
        f"  Sized at    Rs {sizing.capital_to_deploy:,.0f} "
        f"({sizing.quantity} shares)\n"
        f"  Signal ID   {signal_id if signal_id else 'not logged'}\n\n"
        f"WHY QC FAILED:\n{qc_verdict.rationale}\n\n"
        f"{streak_line}\n\n"
        "No order was placed and no Approve/Reject link was issued, because "
        "the thesis was never validated. The signal is recorded as QC_ERROR "
        "for the weekly audit.\n\n"
        "Until QC recovers, every qualifying trade will be blocked this way."
    )
    return send_plain_email(
        subject=f"\u26a0 Trade blocked \u2014 QC unreachable ({ticker})",
        body=body,
    )


def send_qc_down_alert(streak: int) -> bool:
    """
    One ops alert when the consecutive-failure streak first crosses the
    threshold. Sent once per outage, not per cycle: the per-signal alert
    already fires every time a trade is actually lost.
    """
    return send_plain_email(
        subject=f"\U0001f6a8 QC has failed {streak} cycles in a row \u2014 system degraded",
        body=(
            f"The QC fact-checker has now failed {streak} consecutive times.\n\n"
            "Every trade signal that clears the Researcher and the Risk Sizer "
            "is being blocked at the QC gate. The system is generating signals "
            "and shipping none of them.\n\n"
            "Common causes, cheapest first:\n"
            "  - OPENAI_API_KEY quota exhausted (429 insufficient_quota)\n"
            "  - OPENAI_API_KEY missing, revoked, or rotated\n"
            "  - QC_MODEL name not available to the account\n"
            "  - OpenAI API outage\n\n"
            "Check provider health:\n"
            f"  {RAILWAY_URL}/status?secret=<APPROVAL_SECRET>\n\n"
            "This alert is sent once per outage. You will keep receiving a "
            "per-signal alert for each trade that gets blocked."
        ),
    )


# Human-readable explanation per pre-QC drop stage. Keys are the signal
# `status` values written by main.py when a 75%+ candidate is dropped BEFORE
# it ever reaches QC. These used to return silently — the whole point here is
# that a qualifying trade can no longer vanish without a trace or a heads-up.
_DROP_STAGE_BLURB = {
    "DROPPED_SIZER": (
        "the Risk Sizer rejected it (a portfolio rule blocked the position — "
        "e.g. sector cap full, max concurrent positions, weekly drawdown, or "
        "already holding this name)."
    ),
    "DROPPED_NO_PRICE": (
        "a live price could not be fetched from Kite, so the order could not "
        "be sized. This is usually the daily Kite login having expired — tap "
        "the morning 'Login with Kite' email to restore it."
    ),
    "DROPPED_SUBMIN": (
        "after applying all constraints the position came out below the "
        "minimum tradeable size, so it was not worth the transaction cost."
    ),
}


def send_candidate_dropped_alert(signal_id, signal, sizing, status: str) -> bool:
    """
    Fired the moment a candidate that cleared the 75% confidence bar is dropped
    BEFORE reaching QC — at the Risk Sizer or the price/size step.

    Immediate, not batched: like the QC-unreachable alert, a missed trade is
    only actionable while the market is open. No Approve/Reject links — the
    thesis was never QC-validated, so there is nothing safe to approve. This is
    a heads-up, not a decision.
    """
    ticker = signal.ticker
    why = _DROP_STAGE_BLURB.get(status, "it was dropped before QC.")
    system_fault = status == "DROPPED_NO_PRICE"
    sized_line = (
        f"  Sizer said  Rs {sizing.capital_to_deploy:,.0f}\n"
        if getattr(sizing, "capital_to_deploy", 0) else ""
    )
    body = (
        "A trade signal cleared the Researcher's 75% confidence bar, then was "
        "dropped BEFORE the QC fact-checker ran.\n\n"
        "This is NOT a QC rejection and NOT a quiet market — it is a candidate "
        "that would otherwise have gone to QC and possibly to you as an "
        "Approve/Reject alert.\n"
        + "=" * 52 + "\n\n"
        f"  Ticker      {ticker} ({getattr(signal, 'exchange', 'NSE')})\n"
        f"  Direction   {signal.direction}\n"
        f"  Strategy    {signal.strategy_bucket}\n"
        f"  Regime      {signal.regime.value}\n"
        f"  Confidence  {signal.confidence_score:.0f}%\n"
        f"{sized_line}"
        f"  Stage       {status}\n"
        f"  Signal ID   {signal_id if signal_id else 'not logged'}\n\n"
        f"WHY IT WAS DROPPED:\n  {why}\n\n"
        "No order was placed and no Approve/Reject link was issued. The signal "
        f"is recorded as {status} for the daily summary and the weekly audit.\n"
        + (
            "\nACTION: this looks like an infrastructure fault, not a risk "
            "decision — it is worth fixing now so live candidates stop being "
            "dropped.\n" if system_fault else ""
        )
    )
    subject_tag = "⚠ Trade dropped pre-QC"
    return send_plain_email(
        subject=f"{subject_tag} — {status.replace('DROPPED_', '').title()} ({ticker})",
        body=body,
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
            "SELECT id, ticker, exchange, direction, confidence_score, status, "
            "qc_verdict, qc_rationale, created_at "
            "FROM signals WHERE DATE(created_at) = ? ORDER BY created_at",
            (today_ist,)
        ).fetchall()

    # Three outcomes that must never be collapsed into one another:
    #   alerted    a candidate cleared QC and an Approve/Reject alert went out
    #   qc_blocked a candidate cleared 75% and QC genuinely refused it
    #   qc_errored a candidate cleared 75% and QC could not be reached
    # The third is a system fault. Reporting it as "no signals today" is what
    # let an exhausted API quota look exactly like a quiet market.
    qc_errored = [r for r in signals_today if r["status"] == "QC_ERROR"]
    qc_blocked = [r for r in signals_today if r["status"] == "QC_BLOCKED"]
    # Cleared 75% but dropped BEFORE QC (sizer / price / min-size). These used
    # to leave no signals-table row at all, so the summary called them "nothing
    # cleared." Now they are recorded and counted here as a distinct outcome.
    dropped = [r for r in signals_today
               if (r["status"] or "").startswith("DROPPED")]
    alerted = [r for r in signals_today
               if r["status"] not in ("QC_ERROR", "QC_BLOCKED")
               and not (r["status"] or "").startswith("DROPPED")]

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

    def _lines(rows):
        return "\n".join(
            f"  • {r['direction']} {r['ticker']} ({r['exchange']}) "
            f"\u2014 {r['confidence_score']:.0f}% confidence \u2014 {r['status']}"
            for r in rows
        )

    blocks = []

    if qc_errored:
        blocks.append(
            "\U0001f6a8 SYSTEM DEGRADED \u2014 QC WAS UNREACHABLE\n"
            f"{len(qc_errored)} candidate(s) cleared the 75% threshold and were "
            "sized, then blocked because the QC fact-checker could not be "
            "reached. These are NOT rejections \u2014 they are trades lost to an "
            "infrastructure fault.\n"
            f"{_lines(qc_errored)}\n"
            f"  Last error: {(qc_errored[-1]['qc_rationale'] or '')[:200]}"
        )

    if qc_blocked:
        blocks.append(
            "QC reviewed and blocked:\n"
            f"{len(qc_blocked)} candidate(s) cleared 75% but QC returned a "
            "genuine DISAGREE / NEEDS_MORE_DATA. This is QC working as "
            "intended.\n"
            f"{_lines(qc_blocked)}"
        )

    if dropped:
        no_price = [r for r in dropped if r["status"] == "DROPPED_NO_PRICE"]
        header = (
            "⚠ CLEARED 75% BUT DROPPED BEFORE QC\n"
            f"{len(dropped)} candidate(s) cleared the Researcher's 75% bar but "
            "never reached QC — dropped at the Risk Sizer or the price/size "
            "step. These are NOT quiet-market cycles; each was a real "
            "qualifying signal.\n"
        )
        if no_price:
            header += (
                "  At least one was dropped because a live price could not be "
                "fetched (Kite login likely expired) — that is an "
                "infrastructure fault, not a risk decision.\n"
            )
        blocks.append(header + _lines(dropped))

    if alerted:
        blocks.append(f"Signals alerted today:\n{_lines(alerted)}")

    if not blocks:
        blocks.append(
            "No candidate cleared the 75% confidence threshold today.\n"
            "  (QC was not the blocker \u2014 nothing reached it.)"
        )

    signal_block = "\n\n".join(blocks)

    # A missing live price is a system fault (usually an expired Kite login),
    # so a price-driven drop marks the day degraded just like a QC outage does.
    degraded = bool(qc_errored) or any(
        r["status"] == "DROPPED_NO_PRICE" for r in dropped
    )
    body = (
        f"Daily research cycle summary \u2014 {today_ist}"
        f"{'  [SYSTEM DEGRADED]' if degraded else ''}\n"
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
