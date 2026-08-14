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
  python main.py monitor   → run position monitor now
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
        if not portfolio_status.approved:
            log.warning(f"Cycle halted by portfolio risk gate: {portfolio_status.halt_reason}")
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

    if regime_reading.confidence < 60:
        log.info(
            f"Low regime confidence ({regime_reading.confidence:.0f}%) — cycle skipped. "
            f"({regime_reading.rationale[:120]})"
        )
        return

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
        log.error(f"Signal generation failed: {e}", exc_info=True)
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
        log.info(f"Risk Sizer rejected: {sizing.notes}")
        return

    # ----------------------------------------------------------------
    # Step 4: QC / Fact-Checker
    # ----------------------------------------------------------------
    try:
        qc_verdict = validate_signal(signal)
    except Exception as e:
        log.error(f"QC failed: {e}", exc_info=True)
        return

    if qc_verdict.verdict == "DISAGREE":
        log.info(f"QC DISAGREE — {qc_verdict.rationale[:120]}")
        return

    # ----------------------------------------------------------------
    # Step 5: Log signal + send Telegram alert + mirror to Sheets
    # ----------------------------------------------------------------
    try:
        signal_id = log_signal(signal, sizing, qc_verdict)
        update_signal_alert_sent(signal_id)
    except Exception as e:
        log.error(f"Ledger write failed: {e}", exc_info=True)
        return

    try:
        sent = send_trade_alert(signal_id, signal, qc_verdict, sizing)
        if sent:
            log.info(f"Alert sent for signal #{signal_id} ({signal.ticker})")
        else:
            log.error(f"Gmail alert failed for signal #{signal_id}")
    except Exception as e:
        log.error(f"Gmail send failed: {e}", exc_info=True)

    # Mirror to Google Sheets (non-blocking — failure never stops the cycle)
    try:
        from sheets.trade_logger import append_signal, refresh_positions
        append_signal(signal_id, signal, sizing, qc_verdict)
        refresh_positions(raw_positions)  # raw_positions from Step 0
    except Exception as e:
        log.warning(f"Sheets logging failed (non-critical): {e}")


# ---------------------------------------------------------------------------
# Scheduled job wrappers (called by scheduler/market_scheduler.py)
# ---------------------------------------------------------------------------

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
        run_position_monitor()
    elif cmd == "audit":
        run_weekly_audit()
    elif cmd == "eod":
        run_eod_sweep()
    else:
        print(f"Unknown command: {cmd}")
        print("Usage: python main.py [cycle|monitor|audit|eod]")
        sys.exit(1)
