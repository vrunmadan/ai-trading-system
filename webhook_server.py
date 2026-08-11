"""
Webhook server — receives approve/reject actions from Gmail alert links
and dispatches them to the Trader module.

This runs ALONGSIDE the scheduler (as a background thread) so both the
hourly research cycle and the webhook receiver are alive in the same process.
That's the right model for a single-process Railway deployment.

Architecture:
  - Flask receives GET /email_action?action=approve&id=<n>&token=<hmac>
  - Verifies the HMAC token (signed with APPROVAL_SECRET) to prevent forgery
  - Dispatches to alerts.gmail_alert.handle_email_action()
  - handle_email_action() calls execute_trade() if approved, logs if rejected
  - Returns a simple HTML confirmation page the user sees in their phone browser

Setup:
  1. Deploy to Railway (see README — Deployment section)
  2. Set RAILWAY_URL in .env to your Railway deployment URL
  3. Gmail alert links will point to https://your-app.railway.app/email_action
  No webhook registration needed — links are self-contained.
"""

import logging
import os
import threading

from dotenv import load_dotenv
from flask import Flask, jsonify, request

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

app = Flask(__name__)

PORT = int(os.getenv("PORT", 8080))


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "service": "AI Trading System webhook server"})


@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# Email approve/reject endpoint
# ---------------------------------------------------------------------------

def _html_response(title: str, message: str, success: bool) -> str:
    color = "#22c55e" if success else "#ef4444"
    icon  = "✅" if success else "❌"
    return f"""<!DOCTYPE html>
<html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
             display:flex;align-items:center;justify-content:center;
             min-height:100vh;margin:0;background:#f5f5f5">
  <div style="background:#fff;border-radius:12px;padding:36px 28px;
              text-align:center;max-width:360px;box-shadow:0 2px 12px rgba(0,0,0,.1)">
    <div style="font-size:48px;margin-bottom:16px">{icon}</div>
    <h2 style="margin:0 0 8px;color:{color}">{title}</h2>
    <p style="color:#555;margin:0">{message}</p>
  </div>
</body></html>"""


@app.route("/email_action", methods=["GET"])
def email_action():
    """
    User taps Approve or Reject link in their Gmail.
    Query params: action, id, token (HMAC-SHA256 signed with APPROVAL_SECRET).
    """
    from flask import make_response

    action    = request.args.get("action", "")
    signal_id = request.args.get("id", "")
    token     = request.args.get("token", "")

    # Basic param validation
    if action not in ("approve", "reject") or not signal_id or not token:
        html = _html_response("Invalid link", "This link is malformed or expired.", False)
        return make_response(html, 400)

    try:
        signal_id = int(signal_id)
    except ValueError:
        html = _html_response("Invalid link", "Signal ID is not valid.", False)
        return make_response(html, 400)

    # Verify HMAC token
    from alerts.gmail_alert import verify_token, handle_email_action
    if not verify_token(action, signal_id, token):
        log.warning(f"Invalid token for action={action} id={signal_id}")
        html = _html_response("Invalid token", "This link cannot be verified. It may have already been used or is corrupted.", False)
        return make_response(html, 403)

    log.info(f"Email action: {action} signal #{signal_id}")

    try:
        message, success = handle_email_action(action, signal_id)
        title = ("Trade approved" if action == "approve" else "Signal rejected") if success else "Action failed"
        html = _html_response(title, message, success)
        return make_response(html, 200 if success else 500)
    except Exception as e:
        log.error(f"Email action handler error: {e}", exc_info=True)
        html = _html_response("Error", f"Something went wrong: {e}", False)
        return make_response(html, 500)


# ---------------------------------------------------------------------------
# Kite token callback — receives request_token after user logs in via email link
# ---------------------------------------------------------------------------

@app.route("/kite_callback", methods=["GET"])
def kite_callback():
    """
    Zerodha redirects here after the user clicks the morning login link and
    completes Kite Connect login. Captures the request_token, exchanges it
    for an access_token, and saves it to the ledger DB.

    To activate: set your Kite app's redirect URL to:
      https://ai-trading-system-production-6af9.up.railway.app/kite_callback
    at https://developers.kite.trade/apps
    """
    from flask import make_response

    request_token = request.args.get("request_token", "")
    status        = request.args.get("status", "")

    if status != "success" or not request_token:
        log.warning(f"Kite callback: bad params status={status!r} token={request_token!r}")
        html = _html_response(
            "Login failed",
            "Zerodha did not return a valid token. Please try the login link again.",
            False,
        )
        return make_response(html, 400)

    try:
        from kiteconnect import KiteConnect
        kite = KiteConnect(api_key=os.getenv("KITE_API_KEY"))
        session_data = kite.generate_session(
            request_token, api_secret=os.getenv("KITE_API_SECRET")
        )
        access_token = session_data["access_token"]
    except Exception as e:
        log.error(f"Kite generate_session failed: {e}", exc_info=True)
        html = _html_response("Token exchange failed", str(e), False)
        return make_response(html, 500)

    try:
        from ledger.db import save_kite_token
        save_kite_token(access_token)
        log.info(f"Kite access_token refreshed via callback. ({access_token[:8]}...)")
    except Exception as e:
        log.error(f"Failed to save Kite token to DB: {e}", exc_info=True)
        html = _html_response("Save failed", str(e), False)
        return make_response(html, 500)

    html = _html_response(
        "Ready to trade 🟢",
        "Kite token refreshed. Today's trading session is active. You can close this page.",
        True,
    )
    return make_response(html, 200)


# ---------------------------------------------------------------------------
# Debug / test endpoints (safe — read-only or one-shot, no side effects in prod)
# ---------------------------------------------------------------------------

@app.route("/send_test_email", methods=["GET"])
def send_test_email():
    """
    Manually fire the 7:30 AM Kite login email. Useful to verify SMTP works
    after a config change. Protected by APPROVAL_SECRET so it can't be
    accidentally spammed by crawlers.

    Usage:
      https://ai-trading-system-production-6af9.up.railway.app/send_test_email?secret=<APPROVAL_SECRET>
    """
    from flask import make_response
    secret = request.args.get("secret", "")
    expected = os.getenv("APPROVAL_SECRET", "")
    if not expected or secret != expected:
        return make_response(_html_response("Forbidden", "Wrong or missing secret.", False), 403)

    try:
        from alerts.gmail_alert import send_kite_login_email
        ok = send_kite_login_email()
        if ok:
            return make_response(
                _html_response("Email sent ✅", "Kite login email dispatched. Check your inbox.", True), 200
            )
        else:
            return make_response(
                _html_response("Send failed", "send_kite_login_email() returned False. Check Railway logs.", False), 500
            )
    except Exception as e:
        log.error(f"/send_test_email error: {e}", exc_info=True)
        return make_response(_html_response("Error", str(e), False), 500)


# ---------------------------------------------------------------------------
# Scheduler in background thread
# ---------------------------------------------------------------------------

def _start_scheduler():
    """Launch the APScheduler in a daemon thread alongside Flask."""
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.cron import CronTrigger
        import pytz

        IST = pytz.timezone("Asia/Kolkata")
        scheduler = BackgroundScheduler(timezone=IST)

        # Kite login prompt: Mon-Fri 7:30 IST
        scheduler.add_job(
            func=_safe_send_kite_login_email,
            trigger=CronTrigger(day_of_week="mon-fri", hour=7, minute=30, timezone=IST),
            id="kite_login_prompt",
            name="Morning Kite login email",
        )

        # Research cycle: Mon-Fri 9:15, 10:15, ..., 15:15 IST
        scheduler.add_job(
            func=_safe_run_cycle,
            trigger=CronTrigger(
                day_of_week="mon-fri",
                hour="9,10,11,12,13,14,15",
                minute=15,
                timezone=IST,
            ),
            id="research_cycle",
            name="Hourly research cycle",
            max_instances=1,
            coalesce=True,
        )

        # EOD sweep + position monitor: Mon-Fri 15:35 IST
        scheduler.add_job(
            func=_safe_run_eod,
            trigger=CronTrigger(day_of_week="mon-fri", hour=15, minute=35, timezone=IST),
            id="eod_monitor",
            name="EOD sweep and position monitor",
        )

        # Weekly audit: Friday 16:00 IST
        scheduler.add_job(
            func=_safe_run_audit,
            trigger=CronTrigger(day_of_week="fri", hour=16, minute=0, timezone=IST),
            id="weekly_audit",
            name="Weekly Gemini audit",
        )

        scheduler.start()
        log.info("Scheduler started. Jobs: research cycle × 7/day, EOD, weekly audit.")
        return scheduler

    except Exception as e:
        log.error(f"Scheduler failed to start: {e}", exc_info=True)
        return None


def _safe_send_kite_login_email():
    try:
        from alerts.gmail_alert import send_kite_login_email
        send_kite_login_email()
    except Exception as e:
        log.error(f"Kite login email failed: {e}", exc_info=True)


def _safe_run_cycle():
    try:
        from main import run_cycle
        run_cycle()
    except Exception as e:
        log.error(f"Research cycle error: {e}", exc_info=True)


def _safe_run_eod():
    try:
        from main import run_eod_sweep, run_position_monitor
        run_position_monitor()
        run_eod_sweep()
    except Exception as e:
        log.error(f"EOD job error: {e}", exc_info=True)


def _safe_run_audit():
    try:
        from main import run_weekly_audit
        run_weekly_audit()
    except Exception as e:
        log.error(f"Weekly audit error: {e}", exc_info=True)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from ledger.db import init_db

    # Initialize database on startup
    init_db()

    # Start scheduler in background
    scheduler = _start_scheduler()

    log.info(f"Starting webhook server on port {PORT}...")
    log.info("Email approve/reject endpoint: GET /email_action")

    # Flask runs in the main thread; scheduler runs in background
    app.run(host="0.0.0.0", port=PORT, debug=False)
