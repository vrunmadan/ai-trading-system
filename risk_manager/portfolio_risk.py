"""
Portfolio-level risk gate — runs at the START of every research cycle,
before the regime classifier or signal generator even runs.

If any circuit breaker trips, the entire cycle halts immediately.
No new signals are generated. A Gmail alert is sent.

Circuit breakers (all configurable via .env):
  MAX_DRAWDOWN_PCT       - halt if portfolio drops X% below its all-time peak
                           default: 8.0%  (₹10L → halt if value drops below ₹9.2L)
  MAX_DEPLOYED_PCT       - halt if X% of capital is already deployed in open positions
                           default: 65.0% (no new trades if ₹6.5L+ is out)
  WEEKLY_LOSS_LIMIT_INR  - halt the week if realised P&L is below -X
                           default: -15000 (₹15k weekly loss cap)
  MAX_OPEN_POSITIONS     - halt if X or more positions are simultaneously open
                           default: 6
  MAX_SECTOR_PCT         - halt if any single sector exceeds X% of TOTAL capital
                           default: 30.0%
                           NOTE: this is a portfolio circuit breaker. Breaching
                           it halts the ENTIRE cycle, not just that sector.
                           The Risk Sizer has a separate, per-trade sector
                           ceiling (MAX_SECTOR_PCT_OF_CAPITAL). The sizer
                           clamps itself to the tighter of the two so it can
                           never build a position that trips this gate — see
                           the note in risk_sizer/sizer.py.

Portfolio value tracked conservatively as:
  TOTAL_CAPITAL_INR + sum(all realised P&L from closed trades)

Drawdown peak is stored in SQLite and updated whenever portfolio value increases.
This avoids live price fetches in the risk gate (simpler and more reliable).
"""

import logging
import os
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Thresholds — all overridable via .env
# ---------------------------------------------------------------------------
CAPITAL = float(os.getenv("TOTAL_CAPITAL_INR", "1000000"))
MAX_DRAWDOWN_PCT    = float(os.getenv("MAX_DRAWDOWN_PCT",      "8.0"))
MAX_DEPLOYED_PCT    = float(os.getenv("MAX_DEPLOYED_PCT",      "65.0"))
WEEKLY_LOSS_LIMIT   = float(os.getenv("WEEKLY_LOSS_LIMIT_INR", "-15000"))
MAX_OPEN_POSITIONS  = int(os.getenv("MAX_OPEN_POSITIONS",      "6"))
MAX_SECTOR_PCT      = float(os.getenv("MAX_SECTOR_PCT",        "30.0"))


@dataclass
class PortfolioRiskStatus:
    approved: bool              # True → proceed; False → halt cycle
    halt_reason: str            # empty string if approved

    portfolio_value: float      # CAPITAL + all-time realised P&L
    peak_value: float           # highest portfolio value ever reached
    drawdown_pct: float         # (value - peak) / peak * 100  — negative = loss

    deployed_capital: float     # sum of entry_price * qty for all open positions
    deployed_pct: float         # deployed_capital / CAPITAL * 100

    weekly_pnl: float           # this week's realised P&L (Mon–today)
    open_position_count: int
    sector_breaches: list[str] = field(default_factory=list)  # sectors > MAX_SECTOR_PCT


def check_portfolio_risk(
    open_positions: list,   # list of OpenPosition from risk_sizer.sizer
    weekly_pnl: float,      # from ledger.db.get_weekly_pnl()
) -> PortfolioRiskStatus:
    """
    Evaluate all circuit breakers against the current portfolio state.

    Call this FIRST in run_cycle(). If status.approved is False, return
    immediately — do not classify regime or generate signals.
    """
    from ledger.db import get_all_time_pnl, get_update_portfolio_peak

    # ----------------------------------------------------------------
    # Compute current portfolio value and drawdown
    # ----------------------------------------------------------------
    all_time_pnl   = get_all_time_pnl()
    portfolio_value = CAPITAL + all_time_pnl
    peak_value      = get_update_portfolio_peak(portfolio_value)  # updates if new high
    drawdown_pct    = ((portfolio_value - peak_value) / peak_value * 100) if peak_value > 0 else 0.0

    # ----------------------------------------------------------------
    # Compute deployed capital and sector breakdown
    # ----------------------------------------------------------------
    deployed_capital = sum(p.capital_deployed for p in open_positions)
    deployed_pct     = (deployed_capital / CAPITAL * 100) if CAPITAL > 0 else 0.0
    open_count       = len(open_positions)

    sector_map: dict[str, float] = {}
    for p in open_positions:
        sector_map[p.sector] = sector_map.get(p.sector, 0.0) + p.capital_deployed

    sector_breaches = [
        f"{sector} ({amt / CAPITAL * 100:.1f}%)"
        for sector, amt in sector_map.items()
        if (amt / CAPITAL * 100) > MAX_SECTOR_PCT
    ]

    # ----------------------------------------------------------------
    # Circuit breakers — checked in order of severity
    # ----------------------------------------------------------------
    halt_reason = ""

    if drawdown_pct <= -MAX_DRAWDOWN_PCT:
        halt_reason = (
            f"DRAWDOWN CIRCUIT BREAKER: portfolio is {drawdown_pct:.1f}% below peak "
            f"(₹{portfolio_value:,.0f} vs peak ₹{peak_value:,.0f}). "
            f"Limit: -{MAX_DRAWDOWN_PCT}%. No new trades until portfolio recovers."
        )
    elif weekly_pnl <= WEEKLY_LOSS_LIMIT:
        halt_reason = (
            f"WEEKLY LOSS CIRCUIT BREAKER: week P&L is ₹{weekly_pnl:,.0f} "
            f"(limit: ₹{WEEKLY_LOSS_LIMIT:,.0f}). Halting for the rest of the week."
        )
    elif deployed_pct >= MAX_DEPLOYED_PCT:
        halt_reason = (
            f"EXPOSURE LIMIT: {deployed_pct:.1f}% of capital is deployed "
            f"(₹{deployed_capital:,.0f} / ₹{CAPITAL:,.0f}). "
            f"Limit: {MAX_DEPLOYED_PCT}%. Wait for positions to close before adding."
        )
    elif open_count >= MAX_OPEN_POSITIONS:
        halt_reason = (
            f"POSITION COUNT LIMIT: {open_count} open positions "
            f"(limit: {MAX_OPEN_POSITIONS}). Wait for at least one to close."
        )
    elif sector_breaches:
        halt_reason = (
            f"SECTOR CONCENTRATION: {', '.join(sector_breaches)} exceed "
            f"{MAX_SECTOR_PCT}% of capital. No new trades in those sectors."
        )

    approved = not bool(halt_reason)

    status = PortfolioRiskStatus(
        approved=approved,
        halt_reason=halt_reason,
        portfolio_value=portfolio_value,
        peak_value=peak_value,
        drawdown_pct=drawdown_pct,
        deployed_capital=deployed_capital,
        deployed_pct=deployed_pct,
        weekly_pnl=weekly_pnl,
        open_position_count=open_count,
        sector_breaches=sector_breaches,
    )

    if approved:
        log.info(
            f"Portfolio risk gate: OK | "
            f"value ₹{portfolio_value:,.0f} | drawdown {drawdown_pct:.1f}% | "
            f"deployed {deployed_pct:.1f}% | {open_count} positions | "
            f"week P&L ₹{weekly_pnl:,.0f}"
        )
    else:
        log.warning(f"Portfolio risk gate: HALTED — {halt_reason}")
        _send_halt_alert(status)

    return status


def _send_halt_alert(status: PortfolioRiskStatus) -> None:
    """Email an alert when a circuit breaker trips."""
    try:
        from alerts.gmail_alert import send_plain_email
        msg = (
            f"🚨 Portfolio Risk Gate — HALTED\n\n"
            f"{status.halt_reason}\n\n"
            f"Portfolio: ₹{status.portfolio_value:,.0f}  |  "
            f"Peak: ₹{status.peak_value:,.0f}  |  "
            f"Drawdown: {status.drawdown_pct:.1f}%\n"
            f"Deployed: {status.deployed_pct:.1f}%  |  "
            f"Positions: {status.open_position_count}  |  "
            f"Week P&L: ₹{status.weekly_pnl:,.0f}"
        )
        send_plain_email(subject="🚨 Trading halted — circuit breaker tripped", body=msg)
    except Exception as e:
        log.warning(f"Failed to send portfolio halt alert: {e}")
