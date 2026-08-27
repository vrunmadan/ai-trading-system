"""
Orchestrator — wires up the full pipeline and runs each scheduled job.

Pipeline (per research cycle, hourly during market hours):
  classify_regime() → generate_signal() → size_position()
      → validate_signal() → log_signal() → send_trade_alert()

  Trader is triggered separately, from the Gmail approve/reject callback.
  Monitor and Auditor run on their own schedules (see scheduler/).

Running modes:
  python main.py           → run one research cycle now
  python main.py cycle     → same
  python main.py monitor   → reconcile fills, then run position monitor now
  python main.py reconcile → reconcile approved trades against Kite only
  python main.py audit     → run weekly audit now
  python main.py eod       → run EOD missed-opportunity sweep now
"""

import logging
import os
import sys

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

PAPER_MODE = os.getenv("PAPER_MODE", "true").lower() == "true"

# Consecutive failed QC calls before one ops alert goes out. The
# per-signal "trade blocked" alert is unconditional; this only governs
# the extra "QC has been down for N cycles" mail.
QC_ERROR_ALERT_THRESHOLD = int(os.getenv("QC_ERROR_ALERT_THRESHOLD", "3"))

# Hard floor on regime confidence below which the cycle skips entirely.
# Default 0 = never skip on regime confidence: a low-confidence read is treated
# as advisory (flagged on the alert) so signals are never suppressed. Set this
# above 0 only if you deliberately want to stand down in murky markets.
MIN_REGIME_CONFIDENCE = float(os.getenv("MIN_REGIME_CONFIDENCE", "0"))


def _drop_candidate_before_qc(signal, sizing, status: str) -> None:
    """A candidate cleared the 75% bar but was dropped BEFORE QC — at the Risk
    Sizer, the live-price fetch, or the minimum-size check.

    These paths used to `return` silently: no signals-table row, no email, so
    the EOD summary reported "nothing cleared" even though something had. This
    records the drop (so the daily summary and the weekly Auditor see it) and
    sends ONE immediate heads-up per ticker+reason per day (deduped, so an
    hourly-recurring drop does not spam the inbox).
    """
    from ledger.db import log_signal, count_signals_today
    from alerts.gmail_alert import send_candidate_dropped_alert

    try:
        already = count_signals_today(signal.ticker, status)
    except Exception as e:
        log.error(f"Dropped-candidate dedupe check failed: {e}")
        already = 0

    signal_id = None
    try:
        signal_id = log_signal(signal, sizing, status=status)
    except Exception as e:
        log.error(
            f"Could not log {status} signal for {signal.ticker}: {e}",
            exc_info=True,
        )

    if already == 0:
        try:
            send_candidate_dropped_alert(signal_id, signal, sizing, status)
        except Exception as e:
            log.error(
                f"Could not send dropped-candidate alert for {signal.ticker}: {e}",
                exc_info=True,
            )


# ---------------------------------------------------------------------------
# Research cycle (called hourly by scheduler and optionally by run_server.py)
# ---------------------------------------------------------------------------

def run_cycle() -> None:
    """
    One research cycle: classify regime → find signal → size → QC → alert.
    Returns quietly if nothing qualifies — most cycles end here.
    """
    from researcher.regime_classifier import classify_regime
    from researcher.signal_generator import generate_signal
    from risk_sizer.sizer import size_position, OpenPosition
    from qc_factchecker.validator import validate_signal
    from alerts.gmail_alert import send_trade_alert
    from ledger.db import log_signal, update_signal_alert_sent, get_open_positions, get_weekly_pnl

    # ----------------------------------------------------------------
    # Step 0: Portfolio-level risk gate (runs before everything else)
    # ----------------------------------------------------------------
    try:
        from risk_manager.portfolio_risk import check_portfolio_risk
        from universe.loader import load_universe

        raw_positions = get_open_positions()
        try:
            _universe_sector_map = {e.ticker: e.sector for e in load_universe()}
        except Exception:
            _universe_sector_map = {}

        open_positions_for_risk = [
            OpenPosition(
                ticker=p["ticker"],
                sector=_universe_sector_map.get(p["ticker"], "Unknown"),
                capital_deployed=float(p["entry_price"]) * int(p["quantity"]),
            )
            for p in raw_positions
        ]
        weekly_pnl_early = get_weekly_pnl()
        portfolio_status = check_portfolio_risk(open_positions_for_risk, weekly_pnl_early)

        # Advisory flags (exposure / sector / position count) never halt the
        # cycle — they ride along on the alert so the user sees the context and
        # decides. Only a HARD circuit breaker (drawdown / weekly loss) stops
        # research. (User directive 2026-08-26: never suppress a signal on a
        # trade limit.) The hard-halt email is sent inside check_portfolio_risk.
        risk_flags: list[str] = list(getattr(portfolio_status, "advisory_flags", []) or [])
        if not portfolio_status.approved:
            log.warning(f"Cycle halted by HARD circuit breaker: {portfolio_status.halt_reason}")
            return
    except Exception as e:
        log.error(f"Portfolio risk check failed: {e}", exc_info=True)
        # Fail safe — if we can't check risk, don't trade
        return

    # ----------------------------------------------------------------
    # Step 1: Regime classification
    # ----------------------------------------------------------------
    try:
        regime_reading = classify_regime()
    except Exception as e:
        log.error(f"Regime classification failed: {e}", exc_info=True)
        return

    # Regime confidence is ADVISORY, not a gate (user directive 2026-08-26:
    # never suppress a signal). A mixed/low-confidence read no longer skips the
    # cycle — the research runs and any candidate reaches the user, flagged with
    # the regime uncertainty so they can weigh it. A floor can be restored by
    # setting MIN_REGIME_CONFIDENCE > 0 (default 0 = never skip on this).
    if regime_reading.confidence < MIN_REGIME_CONFIDENCE:
        log.info(
            f"Regime confidence {regime_reading.confidence:.0f}% below hard floor "
            f"{MIN_REGIME_CONFIDENCE:.0f}% — cycle skipped. ({regime_reading.rationale[:120]})"
        )
        return

    if regime_reading.confidence < 60:
        flag = (
            f"Low regime confidence ({regime_reading.confidence:.0f}%): the "
            f"{regime_reading.regime.value} read is mixed — signal shown anyway, your call."
        )
        log.info(f"Regime advisory (NOT skipping): {flag}")
        risk_flags.append(flag)

    log.info(
        f"Regime: {regime_reading.regime.value.upper()} | "
        f"confidence: {regime_reading.confidence:.0f}%"
    )

    # ----------------------------------------------------------------
    # Step 2: Signal generation
    # ----------------------------------------------------------------
    try:
        signal = generate_signal(regime_reading)
    except Exception as e:
        log.error(f"Signal generation crashed: {e}", exc_info=True)
        # Never silent: a wholesale crash means candidates may have been found
        # mid-scan and lost. Tell the user — this is a fault, not a quiet market.
        try:
            import traceback as _tb
            from alerts.gmail_alert import send_plain_email
            send_plain_email(
                subject="🚨 Signal generation crashed — cycle aborted",
                body=("The research scan raised an exception and the cycle was "
                      "aborted. Any candidate found before the crash was lost. "
                      "This is a system fault, not a quiet market.\n\n"
                      + _tb.format_exc()[:1500]),
            )
        except Exception as _e:
            log.error(f"Could not send signal-crash alert: {_e}")
        return

    if signal is None:
        return   # No qualifying signal — logged inside generate_signal

    log.info(
        f"Signal: {signal.ticker} / {signal.strategy_bucket} | "
        f"confidence: {signal.confidence_score:.0f}%"
    )

    # ----------------------------------------------------------------
    # Step 3: Risk Sizer
    # ----------------------------------------------------------------
    try:
        # Reuse open_positions and weekly_pnl already fetched in Step 0.
        open_positions = open_positions_for_risk
        weekly_pnl = weekly_pnl_early
        capital = float(os.getenv("TOTAL_CAPITAL_INR", 1_000_000))

        sizing = size_position(
            signal=signal,
            open_positions=open_positions,
            weekly_pnl=weekly_pnl,
            capital=capital,
        )
    except Exception as e:
        log.error(f"Risk sizing failed: {e}", exc_info=True)
        return

    if not sizing.approved:
        # The sizer's weekly-drawdown check is a HARD stop (mirrors the
        # portfolio circuit breaker). Everything else it rejects on — sector
        # cap, position count, free capital, min size — is ADVISORY now: flag
        # it, size it at the user's discretion, and let the candidate go all
        # the way to the alert. The decision to trade is the user's.
        if sizing.notes.startswith("Weekly drawdown"):
            log.warning(f"Cycle halted by sizer HARD stop: {sizing.notes}")
            return
        log.info(f"Sizer advisory (NOT halting): {sizing.notes}")
        from risk_sizer.sizer import suggested_capital

        risk_flags.append(sizing.notes)
        sizing.capital_to_deploy = suggested_capital(signal, capital)
        sizing.notes = (
            "ADVISORY — over a risk limit; sized at your discretion. "
            + sizing.notes
        )

    # ----------------------------------------------------------------
    # Step 3b: Convert the approved rupee amount into a share quantity
    #
    # The Risk Sizer works in rupees; a Kite order needs shares. Without
    # this step sizing.quantity stays 0, log_signal persists that 0, and
    # the approval path reads it back as a 1-share order.
    # Fail-closed: no price, no signal.
    # ----------------------------------------------------------------
    try:
        from trader.kite_client import get_ltp

        ltp = get_ltp(signal.ticker, getattr(signal, "exchange", "NSE"))
    except Exception as e:
        log.error(
            f"Could not fetch LTP for {signal.ticker} — cannot size the order: {e}",
            exc_info=True,
        )
        _drop_candidate_before_qc(signal, sizing, "DROPPED_NO_PRICE")
        return

    if not ltp or ltp <= 0:
        log.error(f"Invalid LTP ({ltp!r}) for {signal.ticker} — cannot size the order.")
        _drop_candidate_before_qc(signal, sizing, "DROPPED_NO_PRICE")
        return

    sizing.quantity = int(sizing.capital_to_deploy // ltp)
    if sizing.quantity < 1:
        log.info(
            f"{signal.ticker}: approved capital \u20b9{sizing.capital_to_deploy:,.0f} "
            f"buys less than one share at \u20b9{ltp:,.2f} — skipping."
        )
        _drop_candidate_before_qc(signal, sizing, "DROPPED_SUBMIN")
        return

    log.info(
        f"Order size: {sizing.quantity} x {signal.ticker} @ \u20b9{ltp:,.2f} "
        f"= \u20b9{sizing.quantity * ltp:,.0f} "
        f"(sizer approved \u20b9{sizing.capital_to_deploy:,.0f})"
    )

    # ----------------------------------------------------------------
    # Step 4: QC / Fact-Checker
    # ----------------------------------------------------------------
    try:
        qc_verdict = validate_signal(signal)
    except Exception as e:
        log.error(f"QC failed: {e}", exc_info=True)
        return

    # ----------------------------------------------------------------
    # QC gate. Both branches below block the trade — that is the correct
    # fail-safe. They are separated because they mean opposite things:
    # a genuine verdict is QC working, an errored one is QC unreachable,
    # and only the second means the system is degraded.
    # ----------------------------------------------------------------
    if qc_verdict.errored:
        # QC never answered. A real, sized, high-confidence candidate is
        # being dropped because of an infrastructure fault, so say so NOW —
        # at EOD is too late to act on a trade.
        from alerts.gmail_alert import send_qc_down_alert, send_qc_unreachable_alert
        from ledger.db import record_qc_error

        streak = record_qc_error()
        log.error(
            f"TRADE BLOCKED BY QC FAILURE — {signal.ticker} "
            f"({signal.strategy_bucket}, {signal.confidence_score:.0f}% confidence, "
            f"₹{sizing.capital_to_deploy:,.0f} sized) was dropped because QC could "
            f"not be reached. Consecutive QC failures: {streak}. "
            f"Reason: {qc_verdict.rationale[:200]}"
        )

        try:
            signal_id = log_signal(signal, sizing, qc_verdict, status="QC_ERROR")
        except Exception as e:
            signal_id = None
            log.error(f"Could not log QC_ERROR signal: {e}", exc_info=True)

        try:
            send_qc_unreachable_alert(signal_id, signal, sizing, qc_verdict, streak)
        except Exception as e:
            log.error(f"Could not send QC-unreachable alert: {e}", exc_info=True)

        # One ops alert when the streak first crosses the threshold. The
        # per-signal alert above still fires every time, so silence here is
        # not silence overall — it just stops duplicate ops mail.
        if streak == QC_ERROR_ALERT_THRESHOLD:
            try:
                send_qc_down_alert(streak)
            except Exception as e:
                log.error(f"Could not send QC-down ops alert: {e}", exc_info=True)
        return

    # QC answered, so the streak is broken regardless of what it decided.
    try:
        from ledger.db import reset_qc_error_streak

        reset_qc_error_streak()
    except Exception:
        pass

    if qc_verdict.verdict != "AGREE":
        # A genuine DISAGREE / NEEDS_MORE_DATA. QC did its job.
        log.info(f"QC {qc_verdict.verdict} — {qc_verdict.rationale[:120]}")
        try:
            log_signal(signal, sizing, qc_verdict, status="QC_BLOCKED")
        except Exception as e:
            log.error(f"Could not log QC_BLOCKED signal: {e}", exc_info=True)
        return

    # ----------------------------------------------------------------
    # Step 5: Log signal + send Gmail alert + mirror to Sheets
    # ----------------------------------------------------------------
    try:
        signal_id = log_signal(signal, sizing, qc_verdict)
        update_signal_alert_sent(signal_id)
    except Exception as e:
        log.error(f"Ledger write failed: {e}", exc_info=True)
        return

    try:
        sent = send_trade_alert(signal_id, signal, qc_verdict, sizing, risk_flags=risk_flags)
        if sent:
            log.info(f"Alert sent for signal #{signal_id} ({signal.ticker})")
        else:
            log.error(f"Gmail alert failed for signal #{signal_id}")
    except Exception as e:
        log.error(f"Gmail send failed: {e}", exc_info=True)

    # Google Sheets mirroring is now handled at the ledger choke points:
    # log_signal() mirrors every signal (any status) to the Signals tab, and
    # log_trade()/close_trade() mirror trades + refresh the Positions tab. No
    # per-cycle call here — that would double-log the alerted signal.


# ---------------------------------------------------------------------------
# Scheduled job wrappers (called by webhook_server.py's _start_scheduler())
# ---------------------------------------------------------------------------

def run_trade_reconciliation() -> None:
    """
    15:35 IST — verify every approved-but-unconfirmed trade against Kite.

    Runs BEFORE the position monitor so stop-losses are evaluated against
    confirmed fills rather than assumed ones.
    """
    from monitor.trade_reconciler import reconcile_pending_trades

    log.info("Running trade reconciliation...")
    try:
        reconcile_pending_trades()
    except Exception as e:
        log.error(f"Trade reconciliation failed: {e}", exc_info=True)


def run_eod_sweep() -> None:
    """15:35 IST — mark unanswered signals as NO_RESPONSE + send daily summary."""
    from alerts.gmail_alert import send_eod_missed_opportunities, send_daily_cycle_summary

    log.info("Running EOD missed-opportunity sweep...")
    try:
        send_eod_missed_opportunities()
    except Exception as e:
        log.error(f"EOD sweep failed: {e}", exc_info=True)

    # Daily summary email — always fires, even when no signals were generated.
    # This gives visibility into whether the system is working or silently failing.
    log.info("Sending daily cycle summary email...")
    try:
        send_daily_cycle_summary()
    except Exception as e:
        log.error(f"Daily summary email failed: {e}", exc_info=True)


def run_position_monitor() -> None:
    """15:35 IST — check all open positions against stop-loss and regime."""
    from monitor.position_monitor import check_open_positions

    log.info("Running position monitor...")
    try:
        check_open_positions()
    except Exception as e:
        log.error(f"Position monitor failed: {e}", exc_info=True)


def run_weekly_audit(week_start: str | None = None, week_end: str | None = None) -> None:
    """Friday 16:00 IST — full week review via Gemini 3.5 Pro."""
    from auditor.weekly_audit import run_weekly_audit as _audit

    log.info("Running weekly audit...")
    try:
        _audit(week_start, week_end)
    except Exception as e:
        log.error(f"Weekly audit failed: {e}", exc_info=True)


# ---------------------------------------------------------------------------
# CLI entry point for manual runs / testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "cycle"
    mode_label = "PAPER" if PAPER_MODE else "LIVE"
    log.info(f"Running in {mode_label} mode. Command: {cmd}")

    if cmd in ("cycle", ""):
        run_cycle()
    elif cmd == "monitor":
        run_trade_reconciliation()
        run_position_monitor()
    elif cmd == "reconcile":
        run_trade_reconciliation()
    elif cmd == "audit":
        run_weekly_audit()
    elif cmd == "eod":
        run_eod_sweep()
    else:
        print(f"Unknown command: {cmd}")
        print("Usage: python main.py [cycle|monitor|reconcile|audit|eod]")
        sys.exit(1)
