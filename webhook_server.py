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
        message, success, kite_url = handle_email_action(action, signal_id)

        # Approve path: redirect straight to Kite's basket order screen.
        # The user's browser opens Kite with the trade pre-filled; they tap Place Order.
        if action == "approve" and kite_url and success:
            from flask import redirect as flask_redirect
            log.info(f"Redirecting to Kite basket: {kite_url[:80]}...")
            return flask_redirect(kite_url, code=302)

        title = ("Signal rejected" if action == "reject" else "Action failed") if success else "Action failed"
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
# Status / health-check endpoint — machine-readable component health
# ---------------------------------------------------------------------------

@app.route("/status", methods=["GET"])
def status():
    """
    Returns JSON health of every system component.
    No secret required — read-only and contains no sensitive values.

    Usage:
      https://ai-trading-system-production-6af9.up.railway.app/status
    """
    import datetime
    import pytz

    checks = {}

    # 1. Email (Resend) config
    resend_key = os.getenv("RESEND_API_KEY", "")
    alert_email = os.getenv("ALERT_EMAIL", "")
    checks["email"] = {
        "resend_api_key_set": bool(resend_key),
        "alert_email_set":    bool(alert_email),
        "alert_email_value":  alert_email or "(not set)",
        "resend_from":        os.getenv("RESEND_FROM", "(not set)"),
        "ok":                 bool(resend_key and alert_email),
    }

    # 2. Kite token
    try:
        from ledger.db import get_kite_token
        token = get_kite_token()
        checks["kite_token"] = {
            "present": bool(token),
            "prefix":  (token[:8] + "...") if token else None,
            "note":    "Token expires at midnight IST each day — must click morning login email",
            "ok":      bool(token),
        }
    except Exception as e:
        checks["kite_token"] = {"ok": False, "error": str(e)}

    # 3. Kite API env vars
    checks["kite_config"] = {
        "api_key_set":    bool(os.getenv("KITE_API_KEY")),
        "api_secret_set": bool(os.getenv("KITE_API_SECRET")),
        "railway_url":    os.getenv("RAILWAY_URL", "(not set)"),
        "ok":             bool(os.getenv("KITE_API_KEY") and os.getenv("KITE_API_SECRET")),
    }

    # 4. DB connectivity
    try:
        from ledger.db import get_weekly_pnl
        get_weekly_pnl()
        checks["database"] = {"ok": True}
    except Exception as e:
        checks["database"] = {"ok": False, "error": str(e)}

    # 5. Recent signals (last 7 days)
    try:
        import sqlite3
        from ledger.db import DB_PATH
        db_path = DB_PATH
        conn = sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM signals WHERE created_at > datetime('now', '-7 days')"
        )
        count = cur.fetchone()[0]
        cur.execute(
            "SELECT ticker, created_at, confidence_score FROM signals "
            "ORDER BY created_at DESC LIMIT 3"
        )
        recent = [{"ticker": r[0], "at": r[1], "confidence": r[2]} for r in cur.fetchall()]
        conn.close()
        checks["signals"] = {"last_7_days": count, "recent": recent, "ok": True}
    except Exception as e:
        checks["signals"] = {"ok": False, "error": str(e)}

    # 6. Screener config
    checks["screener"] = {
        "email_set":    bool(os.getenv("SCREENER_EMAIL")),
        "password_set": bool(os.getenv("SCREENER_PASSWORD")),
        "ok":           bool(os.getenv("SCREENER_EMAIL") and os.getenv("SCREENER_PASSWORD")),
    }

    # 7. Other env vars
    checks["env"] = {
        "paper_mode":         os.getenv("PAPER_MODE", "true"),
        "total_capital_inr":  os.getenv("TOTAL_CAPITAL_INR", "(not set)"),
        "approval_secret_set": bool(os.getenv("APPROVAL_SECRET", "")),
        "google_sheets_set":  bool(os.getenv("GOOGLE_SHEETS_CREDENTIALS_JSON")),
    }

    # Overall status
    critical = ["email", "kite_token", "kite_config", "database"]
    all_ok = all(checks[k].get("ok", False) for k in critical)

    IST = pytz.timezone("Asia/Kolkata")
    now_ist = datetime.datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")

    return jsonify({
        "status":    "ok" if all_ok else "degraded",
        "timestamp": now_ist,
        "checks":    checks,
    }), 200 if all_ok else 503


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
# Cycle diagnostic endpoint — runs regime + indicators for all stocks (NO Claude)
# ---------------------------------------------------------------------------

@app.route("/diagnose_cycle", methods=["GET"])
def diagnose_cycle():
    """
    Runs a full diagnostic cycle WITHOUT calling Claude:
      - Regime classification (real Kite data)
      - Indicator computation for every universe stock
      - Shows which stocks are technically eligible for each strategy

    Fast (~30s). Use this to debug why no signals are appearing.
    Usage: https://ai-trading-system-production-6af9.up.railway.app/diagnose_cycle?secret=<APPROVAL_SECRET>
    """
    from flask import make_response

    secret = request.args.get("secret", "")
    expected = os.getenv("APPROVAL_SECRET", "")
    if not expected or secret != expected:
        return make_response(_html_response("Forbidden", "Wrong or missing secret.", False), 403)

    try:
        import datetime
        import pytz
        from kiteconnect import KiteConnect
        from ledger.db import get_kite_token
        from universe.loader import load_universe
        from researcher.regime_classifier import classify_regime, STRATEGY_BASKETS
        from researcher.signal_generator import _compute_indicators, MIN_CONFIDENCE

        IST = pytz.timezone("Asia/Kolkata")
        now_ist = datetime.datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")

        # Step 1: Kite client
        token = get_kite_token()
        if not token:
            return make_response(_html_response("No Kite token", "Complete today's Kite login first.", False), 503)

        kite = KiteConnect(api_key=os.getenv("KITE_API_KEY"))
        kite.set_access_token(token)

        # Step 2: Regime
        regime_reading = classify_regime()
        baskets = STRATEGY_BASKETS.get(regime_reading.regime, [])

        # Step 3: Instruments maps
        from datetime import date, timedelta
        try:
            bse_rows = kite.instruments("BSE")
            bse_map = {r["tradingsymbol"]: {"token": r["instrument_token"], "exchange": "BSE"}
                       for r in bse_rows if r["instrument_type"] in ("EQ", "BE")}
        except Exception:
            bse_map = {}

        try:
            nse_rows = kite.instruments("NSE")
            nse_map = {r["tradingsymbol"]: {"token": r["instrument_token"], "exchange": "NSE"}
                       for r in nse_rows if r["instrument_type"] in ("EQ", "BE")}
        except Exception:
            nse_map = {}

        instruments_map = {**bse_map, **nse_map}

        # Step 4: Scan universe
        universe = load_universe()
        from_date = (date.today() - timedelta(days=420)).strftime("%Y-%m-%d")
        to_date   = date.today().strftime("%Y-%m-%d")

        rows = []
        for entry in universe:
            info = instruments_map.get(entry.ticker)
            if not info:
                rows.append({"ticker": entry.ticker, "exchange": entry.exchange,
                             "status": "NO_TOKEN", "indicators": None})
                continue
            try:
                hist = kite.historical_data(int(info["token"]), from_date, to_date, "day")
                if len(hist) < 30:
                    rows.append({"ticker": entry.ticker, "exchange": info["exchange"],
                                 "status": f"THIN_HISTORY ({len(hist)} bars)", "indicators": None})
                    continue
                ind = _compute_indicators(hist, entry.ticker)
                rows.append({"ticker": entry.ticker, "exchange": info["exchange"],
                             "status": "OK", "indicators": ind})
            except Exception as e:
                rows.append({"ticker": entry.ticker, "exchange": info["exchange"],
                             "status": f"ERROR: {e}", "indicators": None})

        # Render HTML report
        regime_color = {"bull": "#22c55e", "euphoria": "#16a34a", "sideways": "#f59e0b",
                        "bear": "#ef4444", "extreme_fear_crash": "#7f1d1d"}.get(regime_reading.regime.value, "#888")

        strategy_names = [s["name"] for s in baskets] if baskets else []
        has_strategies = bool(baskets)

        def _flag_cell(ind, strategy_name):
            """Quick rule-of-thumb eligibility check (simplified — Claude does full eval)."""
            if not ind:
                return "<td style='color:#888'>—</td>"
            flags = []
            if strategy_name == "52wk_breakout":
                ok_vol   = ind["volume_ratio_20d"] >= 1.5
                ok_rsi   = 50 <= ind["rsi_14"] <= 70
                ok_st    = ind["supertrend_10_3"] == "GREEN"
                ok_52wk  = ind["pct_from_52wk_high"] >= -1.5
                all_ok   = ok_vol and ok_rsi and ok_st and ok_52wk
                flags = [f"vol {ind['volume_ratio_20d']:.1f}×{'✓' if ok_vol else '✗'}",
                         f"RSI {ind['rsi_14']:.0f}{'✓' if ok_rsi else '✗'}",
                         f"ST {ind['supertrend_10_3']}{'✓' if ok_st else '✗'}",
                         f"52wk {ind['pct_from_52wk_high']:+.1f}%{'✓' if ok_52wk else '✗'}"]
                color = "#22c55e" if all_ok else ("#f59e0b" if sum([ok_vol, ok_rsi, ok_st, ok_52wk]) >= 2 else "#ef4444")
            elif strategy_name == "supertrend_buy":
                ok_flip  = ind["supertrend_just_flipped"]
                ok_rsi   = 40 <= ind["rsi_14"] <= 65
                ok_sma   = ind["above_sma50"]
                all_ok   = ok_flip and ok_rsi and ok_sma
                flags = [f"flipped:{'✓' if ok_flip else '✗'}",
                         f"RSI {ind['rsi_14']:.0f}{'✓' if ok_rsi else '✗'}",
                         f">SMA50:{'✓' if ok_sma else '✗'}"]
                color = "#22c55e" if all_ok else ("#f59e0b" if sum([ok_flip, ok_rsi, ok_sma]) >= 2 else "#ef4444")
            elif strategy_name == "rsi_mean_reversion":
                ok_rsi   = ind["rsi_14"] < 35
                ok_bb    = ind["bollinger_position"] < 25
                ok_52wk  = ind["pct_from_52wk_high"] > -20
                all_ok   = ok_rsi and ok_bb and ok_52wk
                flags = [f"RSI {ind['rsi_14']:.0f}{'✓' if ok_rsi else '✗'}",
                         f"BB {ind['bollinger_position']:.0f}%{'✓' if ok_bb else '✗'}",
                         f"52wk {ind['pct_from_52wk_high']:+.1f}%{'✓' if ok_52wk else '✗'}"]
                color = "#22c55e" if all_ok else ("#f59e0b" if sum([ok_rsi, ok_bb, ok_52wk]) >= 2 else "#ef4444")
            else:
                flags = ["unknown strategy"]
                color = "#888"
            cell_bg = f"background:{color}22"
            return f"<td style='{cell_bg};font-size:11px;padding:4px'>{' | '.join(flags)}</td>"

        # Build table
        table_rows = ""
        for r in rows:
            ind = r["indicators"]
            if r["status"] == "OK" and ind:
                ind_cells = (
                    f"<td style='font-size:11px'>₹{ind['ltp']:,.0f}</td>"
                    f"<td style='font-size:11px'>{ind['rsi_14']:.0f}</td>"
                    f"<td style='font-size:11px'>{ind['supertrend_10_3'][0]}{'↑' if ind['supertrend_just_flipped'] else ''}</td>"
                    f"<td style='font-size:11px'>{ind['volume_ratio_20d']:.1f}×</td>"
                    f"<td style='font-size:11px'>{ind['pct_from_52wk_high']:+.1f}%</td>"
                    f"<td style='font-size:11px'>{ind['bollinger_position']:.0f}%</td>"
                )
                strategy_cells = "".join(_flag_cell(ind, s) for s in strategy_names)
                row_bg = ""
            else:
                ind_cells = f"<td colspan='6' style='color:#ef4444;font-size:11px'>{r['status']}</td>"
                strategy_cells = "<td colspan='99' style='color:#888'>—</td>" if strategy_names else ""
                row_bg = "background:#fff5f5"

            table_rows += (
                f"<tr style='border-bottom:1px solid #e5e7eb;{row_bg}'>"
                f"<td style='font-weight:600;padding:6px 8px'>{r['ticker']}</td>"
                f"<td style='font-size:11px;color:#555'>{r['exchange']}</td>"
                + ind_cells + strategy_cells +
                f"</tr>"
            )

        strategy_headers = "".join(f"<th style='padding:6px 8px;background:#1e293b;color:#fff'>{s}</th>" for s in strategy_names)
        ok_count    = sum(1 for r in rows if r["status"] == "OK")
        no_token    = sum(1 for r in rows if r["status"] == "NO_TOKEN")
        errors      = sum(1 for r in rows if r["status"] not in ("OK", "NO_TOKEN") and "THIN" not in r["status"])

        html = f"""<!DOCTYPE html>
<html><head><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Cycle Diagnostic — {now_ist}</title>
<style>
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;padding:20px;background:#f8fafc;color:#1e293b}}
  h1{{font-size:20px;margin:0 0 4px}} h2{{font-size:15px;margin:16px 0 8px;color:#374151}}
  .badge{{display:inline-block;padding:2px 10px;border-radius:99px;font-size:12px;font-weight:700;color:#fff}}
  table{{width:100%;border-collapse:collapse;background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 1px 4px rgba(0,0,0,.08)}}
  th{{padding:6px 8px;background:#334155;color:#fff;text-align:left;font-size:12px}}
  td{{padding:4px 8px;font-size:12px;vertical-align:middle}}
  .card{{background:#fff;border-radius:8px;padding:16px;margin-bottom:16px;box-shadow:0 1px 4px rgba(0,0,0,.08)}}
  .stat{{display:inline-block;margin-right:24px}}
  .stat-val{{font-size:24px;font-weight:700}} .stat-lbl{{font-size:12px;color:#64748b}}
</style></head>
<body>
<h1>🔍 Cycle Diagnostic</h1>
<p style='color:#64748b;margin:0 0 16px'>{now_ist} — NO Claude calls made (indicators only)</p>

<div class='card'>
  <h2>Regime</h2>
  <span class='badge' style='background:{regime_color}'>{regime_reading.regime.value.upper()}</span>
  &nbsp;<strong>{regime_reading.confidence:.0f}%</strong> confidence
  <p style='margin:8px 0 0;font-size:13px;color:#374151'>{regime_reading.rationale}</p>
  <p style='margin:4px 0 0;font-size:12px;color:#64748b'>
    Nifty vs 200-EMA: <strong>{regime_reading.nifty_vs_ema_pct:+.1f}%</strong> &nbsp;|&nbsp;
    VIX: <strong>{regime_reading.vix:.1f}</strong> &nbsp;|&nbsp;
    Breadth: <strong>{regime_reading.breadth_pct:.0f}%</strong>
  </p>
  {'<p style="margin:8px 0 0;color:#ef4444;font-weight:600">⚠ Low confidence — cycle would be SKIPPED in production</p>' if regime_reading.confidence < 60 else '<p style="margin:8px 0 0;color:#22c55e;font-weight:600">✓ Confidence sufficient — cycle would proceed</p>'}
  {'<p style="margin:4px 0 0;color:#64748b;font-size:12px">Active strategies: ' + ', '.join(strategy_names) + '</p>' if strategy_names else '<p style="margin:8px 0 0;color:#f59e0b;font-weight:600">⚠ No long strategies for this regime — all cycles return immediately</p>'}
</div>

<div class='card'>
  <h2>Universe Scan ({len(rows)} stocks)</h2>
  <div style='margin-bottom:12px'>
    <div class='stat'><div class='stat-val' style='color:#22c55e'>{ok_count}</div><div class='stat-lbl'>Data OK</div></div>
    <div class='stat'><div class='stat-val' style='color:#ef4444'>{no_token}</div><div class='stat-lbl'>No Token</div></div>
    <div class='stat'><div class='stat-val' style='color:#f59e0b'>{errors}</div><div class='stat-lbl'>Errors</div></div>
  </div>
  <table>
    <thead><tr>
      <th>Ticker</th><th>Exch</th><th>LTP</th><th>RSI</th><th>ST</th><th>Vol</th><th>52wk</th><th>BB%</th>
      {strategy_headers if has_strategies else ''}
    </tr></thead>
    <tbody>{table_rows}</tbody>
  </table>
</div>

<p style='font-size:12px;color:#94a3b8'>
  ✓ green = all criteria met (Claude would evaluate this) &nbsp;|&nbsp;
  🟡 amber = 2+ criteria met (marginal) &nbsp;|&nbsp;
  ✗ red = insufficient
</p>
</body></html>"""

        return make_response(html, 200)

    except Exception as e:
        log.error(f"/diagnose_cycle error: {e}", exc_info=True)
        return make_response(_html_response("Error", str(e), False), 500)


# ---------------------------------------------------------------------------
# Cycle history endpoint — shows every stock evaluated, with all scores
# ---------------------------------------------------------------------------

@app.route("/cycle_history", methods=["GET"])
def cycle_history():
    """
    Shows every stock × strategy evaluated across recent cycles, including PASS verdicts.
    This is the "why nothing fired" answer.

    Query params:
      days=N   — how many days back (default 1)
      secret=  — required

    Usage:
      https://ai-trading-system-production-6af9.up.railway.app/cycle_history?secret=<APPROVAL_SECRET>&days=1
    """
    from flask import make_response

    secret = request.args.get("secret", "")
    expected = os.getenv("APPROVAL_SECRET", "")
    if not expected or secret != expected:
        return make_response(_html_response("Forbidden", "Wrong or missing secret.", False), 403)

    try:
        import datetime, pytz
        from ledger.db import get_cycle_log

        days = int(request.args.get("days", "1"))
        rows = get_cycle_log(days=days)

        IST = pytz.timezone("Asia/Kolkata")
        now_ist = datetime.datetime.now(IST).strftime("%Y-%m-%d %H:%M IST")

        if not rows:
            return make_response(
                _html_response(
                    "No data yet",
                    f"No cycle evaluations found in the last {days} day(s). "
                    "Data is stored from the next cycle onward after this deploy.",
                    False,
                ),
                200,
            )

        # Group by cycle_at
        from collections import defaultdict
        cycles = defaultdict(list)
        for r in rows:
            cycles[r["cycle_at"]].append(r)

        verdict_color = {
            "TRADE": "#22c55e", "PASS": "#6b7280",
            "NO_TOKEN": "#ef4444", "ERROR": "#ef4444",
        }

        def _vcolor(v):
            for k, c in verdict_color.items():
                if v.startswith(k):
                    return c
            return "#f59e0b"  # BELOW_THRESHOLD

        def _score_cell(val, low_good=False):
            if val is None:
                return "<td style='color:#ccc'>—</td>"
            v = float(val)
            if low_good:
                color = "#22c55e" if v < 35 else ("#f59e0b" if v < 50 else "#6b7280")
            else:
                color = "#22c55e" if v >= 75 else ("#f59e0b" if v >= 60 else "#ef4444")
            return f"<td style='color:{color};font-weight:600'>{v:.0f}</td>"

        sections = ""
        for cycle_ts in sorted(cycles.keys(), reverse=True):
            cycle_rows = cycles[cycle_ts]
            regime = cycle_rows[0]["regime"].upper() if cycle_rows else "?"
            reg_conf = cycle_rows[0]["regime_confidence"] or 0
            regime_ok = reg_conf >= 60

            table_body = ""
            for r in sorted(cycle_rows, key=lambda x: -(x["confidence_score"] or 0)):
                vc = _vcolor(r["verdict"])
                st = r.get("supertrend", "?") or "?"
                st_short = "G↑" if st == "GREEN" else ("R↓" if st == "RED" else "?")
                table_body += f"""<tr style='border-bottom:1px solid #f1f5f9'>
                  <td style='padding:5px 8px;font-weight:600'>{r['ticker']}</td>
                  <td style='font-size:11px;color:#555'>{r['exchange']}</td>
                  <td style='font-size:11px'>{r['strategy']}</td>
                  <td style='padding:5px 8px;color:{vc};font-weight:700;font-size:12px'>{r['verdict']}</td>
                  {_score_cell(r['technical_score'])}{_score_cell(r['fundamental_score'])}{_score_cell(r['confidence_score'])}
                  <td style='font-size:11px'>{f"{r['rsi']:.0f}" if r['rsi'] else '—'}</td>
                  <td style='font-size:11px'>{st_short}</td>
                  <td style='font-size:11px'>{f"{r['volume_ratio']:.1f}×" if r['volume_ratio'] else '—'}</td>
                  <td style='font-size:11px'>{f"{r['pct_from_52wk_high']:+.1f}%" if r['pct_from_52wk_high'] is not None else '—'}</td>
                  <td style='font-size:11px;color:#64748b;max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap'>{(r['rationale'] or '')[:120]}</td>
                </tr>"""

            trade_count = sum(1 for r in cycle_rows if r["verdict"] == "TRADE")
            best_score  = max((r["confidence_score"] or 0) for r in cycle_rows)

            sections += f"""
<div style='background:#fff;border-radius:8px;padding:16px;margin-bottom:20px;
            box-shadow:0 1px 4px rgba(0,0,0,.08)'>
  <div style='display:flex;align-items:center;gap:12px;margin-bottom:12px'>
    <strong style='font-size:15px'>{cycle_ts}</strong>
    <span style='padding:2px 10px;border-radius:99px;font-size:12px;font-weight:700;color:#fff;
                 background:{"#22c55e" if regime_ok else "#ef4444"}'>{regime}</span>
    <span style='font-size:12px;color:#64748b'>{reg_conf:.0f}% confidence
      {"✓" if regime_ok else "⚠ SKIPPED (< 60%)"}</span>
    <span style='font-size:12px;color:#64748b;margin-left:auto'>
      {len(cycle_rows)} evaluated · best score: <strong>{best_score:.0f}%</strong> ·
      signals above 75%: <strong style='color:{"#22c55e" if trade_count else "#6b7280"}'>{trade_count}</strong>
    </span>
  </div>
  <div style='overflow-x:auto'>
  <table style='width:100%;border-collapse:collapse;font-size:12px'>
    <thead><tr style='background:#f8fafc'>
      <th style='padding:6px 8px;text-align:left'>Ticker</th>
      <th style='padding:6px 8px;text-align:left'>Exch</th>
      <th style='padding:6px 8px;text-align:left'>Strategy</th>
      <th style='padding:6px 8px;text-align:left'>Verdict</th>
      <th style='padding:6px 8px'>Tech</th>
      <th style='padding:6px 8px'>Fund</th>
      <th style='padding:6px 8px'>Conf</th>
      <th style='padding:6px 8px'>RSI</th>
      <th style='padding:6px 8px'>ST</th>
      <th style='padding:6px 8px'>Vol</th>
      <th style='padding:6px 8px'>52wk</th>
      <th style='padding:6px 8px;text-align:left'>Rationale</th>
    </tr></thead>
    <tbody>{table_body}</tbody>
  </table>
  </div>
</div>"""

        html = f"""<!DOCTYPE html>
<html><head><meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Cycle History</title>
<style>body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
  padding:20px;background:#f8fafc;color:#1e293b}}
h1{{font-size:20px;margin:0 0 4px}}
th{{text-align:center}}</style></head>
<body>
<h1>📊 Cycle Evaluation History</h1>
<p style='color:#64748b;margin:0 0 20px'>Last {days} day(s) — generated {now_ist}<br>
<strong style='color:#22c55e'>Green ≥ 75</strong> ·
<strong style='color:#f59e0b'>Amber 60–74</strong> ·
<strong style='color:#ef4444'>Red &lt; 60</strong> (confidence column = threshold)</p>
{sections}
</body></html>"""

        return make_response(html, 200)

    except Exception as e:
        log.error(f"/cycle_history error: {e}", exc_info=True)
        return make_response(_html_response("Error", str(e), False), 500)


# ---------------------------------------------------------------------------
# Universe diagnostic endpoint — checks CSV vs Kite live instrument list
# ---------------------------------------------------------------------------

@app.route("/diagnose_universe", methods=["GET"])
def diagnose_universe():
    """
    Compares universe.csv tickers against live NSE + BSE instruments from Kite.
    Returns an HTML report showing which stocks match, which are BSE-only,
    which have possible symbol corrections, and which aren't found anywhere.

    Requires a valid Kite access token (captured from today's morning login).
    Usage: https://ai-trading-system-production-6af9.up.railway.app/diagnose_universe?secret=<APPROVAL_SECRET>
    """
    from flask import make_response

    secret = request.args.get("secret", "")
    expected = os.getenv("APPROVAL_SECRET", "")
    if not expected or secret != expected:
        return make_response(_html_response("Forbidden", "Wrong or missing secret.", False), 403)

    try:
        from kiteconnect import KiteConnect
        from ledger.db import get_kite_token
        from universe.diagnose_universe import run_diagnosis, format_html_report

        api_key = os.getenv("KITE_API_KEY")
        access_token = get_kite_token()
        if not access_token:
            return make_response(
                _html_response("No token", "No Kite access token found. Complete today's morning login first.", False), 503
            )

        kite = KiteConnect(api_key=api_key)
        kite.set_access_token(access_token)

        results = run_diagnosis(kite=kite)
        html = format_html_report(results)
        return make_response(html, 200)

    except Exception as e:
        log.error(f"/diagnose_universe error: {e}", exc_info=True)
        return make_response(_html_response("Error", str(e), False), 500)


# ---------------------------------------------------------------------------
# Universe refresh — manual trigger endpoint
# ---------------------------------------------------------------------------

@app.route("/refresh_universe", methods=["GET"])
def refresh_universe():
    """
    Runs the full Screener.in discovery + validation job on demand.
    Same job as the Sunday 8pm IST scheduler, just triggered manually.

    Returns an HTML report inline (no email — you're already looking at it).

    Usage:
      https://ai-trading-system-production-6af9.up.railway.app/refresh_universe?secret=<APPROVAL_SECRET>
    """
    from flask import make_response

    secret = request.args.get("secret", "")
    expected = os.getenv("APPROVAL_SECRET", "")
    if not expected or secret != expected:
        return make_response(_html_response("Forbidden", "Wrong or missing secret.", False), 403)

    try:
        from universe.screener_client import build_client_from_env
        from universe.universe_refresher import run_universe_refresh, format_html_report

        client  = build_client_from_env()
        results = run_universe_refresh(screener_client=client)
        html    = format_html_report(results)
        return make_response(html, 200)

    except EnvironmentError as e:
        # Missing SCREENER_EMAIL / SCREENER_PASSWORD
        return make_response(
            _html_response(
                "Screener not configured",
                str(e) + " Set SCREENER_EMAIL and SCREENER_PASSWORD in Railway → Variables.",
                False,
            ),
            503,
        )
    except Exception as e:
        log.error(f"/refresh_universe error: {e}", exc_info=True)
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

        # Universe refresh + discovery: Sunday 20:00 IST
        # Runs a fundamental screen across ALL ~5000+ listed NSE+BSE stocks,
        # surfaces new candidates and flags degraded universe stocks.
        scheduler.add_job(
            func=_safe_run_universe_refresh,
            trigger=CronTrigger(day_of_week="sun", hour=20, minute=0, timezone=IST),
            id="universe_refresh",
            name="Weekly universe discovery (Screener.in)",
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


def _safe_run_universe_refresh():
    try:
        from universe.universe_refresher import run_and_email
        run_and_email()
    except Exception as e:
        log.error(f"Universe refresh error: {e}", exc_info=True)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _startup_health_check():
    """
    Log a clear summary of component health at startup.
    Problems here show up at the top of Railway logs — easy to spot.
    """
    issues = []

    resend_key  = os.getenv("RESEND_API_KEY", "")
    alert_email = os.getenv("ALERT_EMAIL", "")
    kite_key    = os.getenv("KITE_API_KEY", "")
    kite_secret = os.getenv("KITE_API_SECRET", "")

    if not resend_key:
        issues.append("RESEND_API_KEY not set — email alerts will fail")
    if not alert_email:
        issues.append("ALERT_EMAIL not set — no destination for alerts")
    if not kite_key or not kite_secret:
        issues.append("KITE_API_KEY / KITE_API_SECRET not set — Kite integration broken")
    if not os.getenv("APPROVAL_SECRET"):
        issues.append("APPROVAL_SECRET not set — approve/reject links will use default (insecure)")

    resend_from = os.getenv("RESEND_FROM", "AI Trading System <onboarding@resend.dev>")
    if "onboarding@resend.dev" in resend_from:
        issues.append(
            "RESEND_FROM uses onboarding@resend.dev — this only works to the email you "
            "used to sign up for Resend. If alerts aren't arriving, verify a domain at "
            "resend.com/domains and set RESEND_FROM=trading@yourdomain.com"
        )

    if issues:
        log.warning("=" * 60)
        log.warning("STARTUP HEALTH CHECK — ISSUES FOUND:")
        for i, issue in enumerate(issues, 1):
            log.warning(f"  {i}. {issue}")
        log.warning("=" * 60)
    else:
        log.info("Startup health check: all critical env vars present ✓")

    log.info(f"  ALERT_EMAIL   = {alert_email or '(not set)'}")
    log.info(f"  RESEND_FROM   = {resend_from}")
    log.info(f"  RAILWAY_URL   = {os.getenv('RAILWAY_URL', '(not set)')}")
    log.info(f"  PAPER_MODE    = {os.getenv('PAPER_MODE', 'true')}")
    log.info(f"  TOTAL_CAPITAL = ₹{os.getenv('TOTAL_CAPITAL_INR', '(not set)')}")


if __name__ == "__main__":
    from ledger.db import init_db

    # Initialize database on startup
    init_db()

    # Run startup health check — problems appear at top of Railway logs
    _startup_health_check()

    # Start scheduler in background
    scheduler = _start_scheduler()

    log.info(f"Starting webhook server on port {PORT}...")
    log.info("Email approve/reject endpoint: GET /email_action")

    # Flask runs in the main thread; scheduler runs in background
    app.run(host="0.0.0.0", port=PORT, debug=False)
