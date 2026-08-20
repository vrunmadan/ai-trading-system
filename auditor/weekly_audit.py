"""
Auditor — weekly review running on Gemini 3.5 Pro (google-generativeai SDK).

Why Gemini 3.5 Pro:
  - Third distinct model family (Anthropic → OpenAI → Google) eliminates
    correlated failure modes across the full pipeline.
  - 2M token context window: can take the entire week of signals, trades,
    and position checks in ONE prompt without chunking. A chunked audit
    misses cross-signal patterns; a full-context audit catches them.

Two mandatory jobs (kept strictly separate):
  1. Confidence calibration: for each confidence bucket (60-74%, 75-84%,
     85%+), what was the actual win rate? Miscalibrated confidence means
     the whole pipeline's position sizing is wrong.
  2. Missed-opportunity / hypothesis review: what patterns appear in what
     was NOT taken (NO_RESPONSE, REJECTED, SKIPPED, NOT_EXECUTED)? Hypotheses go to
     hypothesis_backlog — NOT directly to the live Researcher prompt.
     Paper-test before live adoption. Always.

Output is written to the weekly_audits table. A Gmail alert fires only
when the auditor raises a calibration flag (serious systemic issue).
"""

import json
import logging
import os
from datetime import date, timedelta

log = logging.getLogger(__name__)

AUDITOR_MODEL = os.getenv("AUDITOR_MODEL", "gemini-3.5-pro")


AUDITOR_SYSTEM_PROMPT = """You are the weekly performance auditor for an AI-powered Indian equity
trading system. You receive the complete records for the week: every signal generated,
every trade placed or missed, and every daily position check.

Your two mandatory tasks:

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TASK 1: CONFIDENCE CALIBRATION
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Group signals by confidence bucket: 60-74%, 75-84%, 85%+.
For each bucket: count signals, count closed trades, count wins (pnl > 0), compute win rate.
A win rate materially below the confidence bucket label is a calibration failure.
Example: if 85%+ confidence signals are only winning 45% of the time, the confidence
scores are decorative numbers, not predictive probabilities — the scoring rubric needs
to be revised. Flag this. It's the most important systemic finding.

If sample is < 3 closed trades in a bucket, say so explicitly and do not over-fit.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TASK 2: MISSED OPPORTUNITY REVIEW
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Look at signals with status: NO_RESPONSE, REJECTED, SKIPPED, or NOT_EXECUTED
(NOT_EXECUTED = the user approved it but the order never reached the market).
Signals with status EXECUTED had a confirmed fill; APPROVED means the fill
was never verified, so do not count those as taken trades.
Describe any patterns you see. Were there genuine missed profits? Was the
rejection/non-response systematically correlated with time of day, regime,
or strategy type?

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TASK 3: HYPOTHESIS BACKLOG
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Based only on what this week's data shows, generate 1-3 specific, testable
hypotheses. Each must include the test condition (what data would confirm
or reject it). These go into the backlog for paper testing — NOT live adoption.

RULES:
- Never recommend live system changes directly. Hypotheses → paper test → evidence → live.
- Separate observations from inferences. Label inferences.
- Small sample caveat: if < 5 signals total, explicitly warn that findings are not meaningful.
- A specific hypothesis: "Raise confidence threshold for sideways/rsi_mean_reversion to 82%
  because all 3 RSI bounce signals this week needed a 2-day RSI reversal confirmation
  that single-day RSI didn't show" is useful. "Be more selective" is not.

Output ONLY valid JSON (no markdown, no prose outside the JSON):
{
  "summary": "3-4 sentence narrative of the week — what happened, key learnings",
  "confidence_bucket_analysis": {
    "60_74_pct": {
      "signals": 0, "closed_trades": 0, "wins": 0, "win_rate_pct": null,
      "notes": "sample too small" or "calibrated" or "MISCALIBRATED: expected 67%, got X%"
    },
    "75_84_pct": {
      "signals": 0, "closed_trades": 0, "wins": 0, "win_rate_pct": null, "notes": ""
    },
    "85_plus_pct": {
      "signals": 0, "closed_trades": 0, "wins": 0, "win_rate_pct": null, "notes": ""
    }
  },
  "missed_opportunities": "narrative analysis of NO_RESPONSE / REJECTED / SKIPPED / NOT_EXECUTED signals",
  "hypothesis_backlog": [
    {
      "hypothesis": "...",
      "evidence_basis": "specific data points from this week that support it",
      "test_condition": "what sample/criteria would confirm or reject this over the next 4 weeks"
    }
  ],
  "calibration_flag": false
}"""


def run_weekly_audit(week_start: str | None = None, week_end: str | None = None) -> None:
    """
    Pulls the full week from the ledger, sends to Gemini 3.5 Pro, writes results.

    Args:
        week_start: "YYYY-MM-DD" — defaults to last Monday
        week_end:   "YYYY-MM-DD" — defaults to last Friday
    """
    import google.generativeai as genai
    from ledger.db import get_signals_for_week, get_trades_for_week, get_db

    # Default to the just-completed Mon-Fri week
    if not week_start or not week_end:
        today = date.today()
        days_since_monday = today.weekday()  # 0=Mon, 4=Fri
        last_monday = today - timedelta(days=days_since_monday)
        last_friday = last_monday + timedelta(days=4)
        week_start = last_monday.strftime("%Y-%m-%d")
        week_end = last_friday.strftime("%Y-%m-%d")

    log.info(f"Weekly audit: {week_start} → {week_end} using {AUDITOR_MODEL}")

    # Pull all ledger data for the week
    signals = get_signals_for_week(week_start, week_end)
    trades = get_trades_for_week(week_start, week_end)

    trade_ids = [t["id"] for t in trades]
    if trade_ids:
        with get_db() as conn:
            placeholders = ",".join("?" * len(trade_ids))
            rows = conn.execute(
                f"SELECT * FROM position_checks WHERE trade_id IN ({placeholders})",
                trade_ids,
            ).fetchall()
            position_checks = [dict(r) for r in rows]
    else:
        position_checks = []

    if not signals and not trades:
        log.info(f"No data for {week_start}–{week_end} — skipping audit.")
        return

    log.info(
        f"Audit data: {len(signals)} signals, {len(trades)} trades, "
        f"{len(position_checks)} position checks"
    )

    # Build the prompt data block
    data_block = f"""=== WEEKLY TRADING DATA: {week_start} to {week_end} ===

SIGNALS ({len(signals)} total):
{json.dumps(signals, indent=2, default=str)}

TRADES ({len(trades)} total):
{json.dumps(trades, indent=2, default=str)}

DAILY POSITION CHECKS ({len(position_checks)} total):
{json.dumps(position_checks, indent=2, default=str)}

Please analyze and return your audit as JSON."""

    # Call Gemini 3.5 Pro
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        log.error("GOOGLE_API_KEY not set — cannot run weekly audit")
        return

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(
        AUDITOR_MODEL,
        system_instruction=AUDITOR_SYSTEM_PROMPT,
    )

    raw_text = ""
    try:
        response = model.generate_content(data_block)
        raw_text = response.text.strip()

        # Strip markdown fences if present
        if "```" in raw_text:
            parts = raw_text.split("```")
            raw_text = parts[1] if len(parts) > 1 else parts[0]
            if raw_text.startswith("json"):
                raw_text = raw_text[4:].strip()

        audit_data = json.loads(raw_text)
        log.info(f"Audit complete. Calibration flag: {audit_data.get('calibration_flag')}")

    except json.JSONDecodeError as e:
        log.error(f"Auditor returned invalid JSON: {e}\nRaw: {raw_text[:500]}")
        audit_data = {
            "summary": f"Audit parse error: {e}. Raw output (first 500 chars): {raw_text[:500]}",
            "confidence_bucket_analysis": {},
            "missed_opportunities": "Parse error — see logs",
            "hypothesis_backlog": [],
            "calibration_flag": False,
        }
    except Exception as e:
        log.error(f"Auditor Gemini API call failed: {e}")
        audit_data = {
            "summary": f"Audit API error: {type(e).__name__}: {e}",
            "confidence_bucket_analysis": {},
            "missed_opportunities": "API error — see logs",
            "hypothesis_backlog": [],
            "calibration_flag": False,
        }

    # Write to weekly_audits table
    try:
        from ledger.db import get_db
        with get_db() as conn:
            conn.execute(
                """
                INSERT INTO weekly_audits (
                    week_start, week_end, summary,
                    confidence_bucket_analysis, missed_opportunities,
                    hypothesis_backlog, model_used
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    week_start,
                    week_end,
                    audit_data.get("summary", ""),
                    json.dumps(audit_data.get("confidence_bucket_analysis", {})),
                    audit_data.get("missed_opportunities", ""),
                    json.dumps(audit_data.get("hypothesis_backlog", [])),
                    AUDITOR_MODEL,
                ),
            )
        log.info(f"Audit written to ledger for {week_start}–{week_end}")
    except Exception as e:
        log.error(f"Failed to write audit to ledger: {e}")

    # Mirror audit to Google Sheets (non-blocking)
    try:
        from sheets.trade_logger import append_audit
        append_audit(
            week_start=week_start,
            week_end=week_end,
            summary=audit_data.get("summary", ""),
            confidence_analysis=json.dumps(audit_data.get("confidence_bucket_analysis", {})),
            missed_opportunities=audit_data.get("missed_opportunities", ""),
            hypothesis_backlog=json.dumps(audit_data.get("hypothesis_backlog", [])),
            model_used=AUDITOR_MODEL,
        )
    except Exception as e:
        log.warning(f"Sheets audit log failed (non-critical): {e}")

    # Calibration flag → Gmail alert
    if audit_data.get("calibration_flag"):
        log.warning("⚠ CALIBRATION FLAG raised by Auditor — sending Gmail alert")
        _send_calibration_alert(
            week_start, week_end,
            audit_data.get("summary", ""),
            audit_data.get("confidence_bucket_analysis", {}),
        )


def _send_calibration_alert(
    week_start: str, week_end: str, summary: str, bucket_analysis: dict
) -> None:
    """Email alert when auditor raises a calibration flag."""
    try:
        from alerts.gmail_alert import send_plain_email
    except Exception:
        return

    bucket_text = "\n".join(
        f"  {bucket}: {data.get('notes', '')}"
        for bucket, data in bucket_analysis.items()
        if isinstance(data, dict)
    )

    msg = (
        f"Weekly Audit — Calibration Flag Raised\n"
        f"Week: {week_start} → {week_end}\n\n"
        f"{summary}\n\n"
        f"Confidence buckets:\n{bucket_text}\n\n"
        f"Check weekly_audits table → hypothesis_backlog for details."
    )

    try:
        send_plain_email(subject=f"⚠️ Weekly Audit — Calibration flag ({week_start})", body=msg)
    except Exception as e:
        log.error(f"Failed to send calibration alert: {e}")
