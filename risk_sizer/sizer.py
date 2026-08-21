"""
Risk Sizer — full implementation. No LLM calls, ever.

Position sizing is deterministic math. An LLM deciding your size is like
asking your analyst to also run the compliance desk — different job,
different standards. This module is the compliance desk.
"""

import logging
import os
from dataclasses import dataclass

# Hard limits — pulled from .env, with conservative defaults
TOTAL_CAPITAL = float(os.getenv("TOTAL_CAPITAL_INR", 1_000_000))
MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT_OF_CAPITAL", 20))   # max 20% per trade
_SIZER_SECTOR_PCT = float(os.getenv("MAX_SECTOR_PCT_OF_CAPITAL", 35))   # per-trade ceiling
_GATE_SECTOR_PCT = float(os.getenv("MAX_SECTOR_PCT", 30))               # portfolio halt threshold

# There are two sector limits in this system and they govern different things:
#
#   MAX_SECTOR_PCT            (risk_manager/portfolio_risk.py, default 30%)
#       A portfolio circuit breaker. If any sector exceeds it, the ENTIRE
#       cycle halts — including trades in completely unrelated sectors.
#
#   MAX_SECTOR_PCT_OF_CAPITAL (here, default 35%)
#       A per-trade ceiling. Caps how much a single new position may add.
#
# Configured as 35 > 30, the per-trade ceiling was looser than the halt
# threshold, which opens a self-defeating band:
#
#   sector at 33% of capital -> this sizer happily allows more
#                            -> next cycle the portfolio gate sees 33% > 30%
#                            -> every trade halts, in every sector
#
# The sizer would be manufacturing the outage. So the EFFECTIVE ceiling is the
# tighter of the two: the sizer can never push a sector past the point where
# the gate would halt the system. Both variables stay configurable, and a
# mismatch is logged rather than silently reconciled.
EFFECTIVE_SECTOR_PCT = min(_SIZER_SECTOR_PCT, _GATE_SECTOR_PCT)
MAX_SECTOR_PCT = EFFECTIVE_SECTOR_PCT   # name kept for existing callers/tests

if _SIZER_SECTOR_PCT > _GATE_SECTOR_PCT:
    logging.getLogger(__name__).warning(
        "Sector cap mismatch: MAX_SECTOR_PCT_OF_CAPITAL=%.1f%% is looser than "
        "MAX_SECTOR_PCT=%.1f%%, which would let the sizer build a position "
        "that halts the next cycle. Using the tighter %.1f%%. Set them equal "
        "to silence this.",
        _SIZER_SECTOR_PCT, _GATE_SECTOR_PCT, EFFECTIVE_SECTOR_PCT,
    )
MAX_WEEKLY_DRAWDOWN_PCT = float(os.getenv("MAX_WEEKLY_DRAWDOWN_PCT", 10))
MAX_CONCURRENT_POSITIONS = int(os.getenv("MAX_CONCURRENT_POSITIONS", 5))
MIN_POSITION_INR = float(os.getenv("MIN_POSITION_INR", 10_000))          # below this, not worth cost


@dataclass
class OpenPosition:
    ticker: str
    sector: str
    capital_deployed: float  # INR currently at risk


@dataclass
class SizingDecision:
    approved: bool
    capital_to_deploy: float  # INR — run_cycle Step 3b converts this to shares at live LTP
    quantity: int             # 0 from the sizer; run_cycle Step 3b fills it at live LTP
    notes: str


def size_position(
    signal,
    open_positions: list[OpenPosition],
    weekly_pnl: float,
    capital: float = TOTAL_CAPITAL,
) -> SizingDecision:
    """
    Six checks, in order of severity. Returns on first failure.
    If all pass, returns confidence-scaled position size.

    Args:
        signal:          TradeSignal from the Researcher
        open_positions:  list of currently open trades (from ledger)
        weekly_pnl:      this week's realised + unrealised P&L in INR
        capital:         total capital base (default from .env)
    """

    # -----------------------------------------------------------------------
    # Check 1: Weekly drawdown budget
    # -----------------------------------------------------------------------
    drawdown_limit = capital * (MAX_WEEKLY_DRAWDOWN_PCT / 100)
    if weekly_pnl < -drawdown_limit:
        return SizingDecision(
            approved=False, capital_to_deploy=0, quantity=0,
            notes=(
                f"Weekly drawdown limit hit. "
                f"P&L this week: ₹{weekly_pnl:,.0f} | Limit: -₹{drawdown_limit:,.0f}. "
                f"No new trades until next Monday."
            ),
        )

    # -----------------------------------------------------------------------
    # Check 2: Concurrent position cap
    # -----------------------------------------------------------------------
    if len(open_positions) >= MAX_CONCURRENT_POSITIONS:
        return SizingDecision(
            approved=False, capital_to_deploy=0, quantity=0,
            notes=(
                f"Already holding {len(open_positions)} positions "
                f"(max {MAX_CONCURRENT_POSITIONS}). Wait for an exit before entering."
            ),
        )

    # -----------------------------------------------------------------------
    # Check 3: No pyramiding — already holding this ticker
    # -----------------------------------------------------------------------
    open_tickers = {p.ticker for p in open_positions}
    if signal.ticker in open_tickers:
        return SizingDecision(
            approved=False, capital_to_deploy=0, quantity=0,
            notes=f"Already holding {signal.ticker}. No adding to existing positions in v1.",
        )

    # -----------------------------------------------------------------------
    # Check 4: Sector concentration
    # Hidden correlation risk: "different" momentum picks in the same sector
    # are not different bets — they're the same directional trade wearing
    # different name tags.
    # -----------------------------------------------------------------------
    sector_deployed = sum(
        p.capital_deployed for p in open_positions if p.sector == signal.sector
    )
    sector_cap = capital * (MAX_SECTOR_PCT / 100)
    if sector_deployed >= sector_cap:
        return SizingDecision(
            approved=False, capital_to_deploy=0, quantity=0,
            notes=(
                f"Sector cap hit: {signal.sector} already has "
                f"₹{sector_deployed:,.0f} deployed (limit ₹{sector_cap:,.0f}). "
                f"Rejecting to avoid correlated exposure."
            ),
        )

    # -----------------------------------------------------------------------
    # Check 5: Free capital available
    # -----------------------------------------------------------------------
    total_deployed = sum(p.capital_deployed for p in open_positions)
    free_capital = capital - total_deployed
    if free_capital < MIN_POSITION_INR:
        return SizingDecision(
            approved=False, capital_to_deploy=0, quantity=0,
            notes=f"Only ₹{free_capital:,.0f} free capital — below minimum position size ₹{MIN_POSITION_INR:,.0f}.",
        )

    # -----------------------------------------------------------------------
    # Sizing: fixed fractional, scaled by confidence
    #
    # Base position = MAX_POSITION_PCT% of capital (e.g. 20% = ₹2L on 10L)
    # Confidence scaling:
    #   90%+ confidence → 100% of base
    #   80%  confidence →  89% of base
    #   70%  confidence →  78% of base  (roughly linear)
    # Below 70% the Researcher shouldn't be sending signals at all.
    # -----------------------------------------------------------------------
    base_position = capital * (MAX_POSITION_PCT / 100)
    confidence_pct = max(70.0, min(100.0, signal.confidence_score))
    confidence_scale = (confidence_pct - 70) / 30 * 0.40 + 0.60  # maps 70→0.60, 100→1.00
    target = base_position * confidence_scale

    # Don't exceed free capital or remaining sector headroom
    sector_headroom = sector_cap - sector_deployed
    capital_to_deploy = min(target, free_capital * 0.95, sector_headroom)
    capital_to_deploy = round(capital_to_deploy, 2)

    if capital_to_deploy < MIN_POSITION_INR:
        return SizingDecision(
            approved=False, capital_to_deploy=0, quantity=0,
            notes=(
                f"Computed position ₹{capital_to_deploy:,.0f} is below minimum "
                f"₹{MIN_POSITION_INR:,.0f} after constraints. Not worth transaction cost."
            ),
        )

    return SizingDecision(
        approved=True,
        capital_to_deploy=capital_to_deploy,
        quantity=0,  # placeholder: run_cycle Step 3b sets this from the live LTP
        notes=(
            f"Approved ₹{capital_to_deploy:,.0f} "
            f"({capital_to_deploy / capital * 100:.1f}% of capital). "
            f"Confidence {signal.confidence_score:.0f}% → scale {confidence_scale:.0%}. "
            f"Sector {signal.sector}: "
            f"₹{sector_deployed:,.0f} + ₹{capital_to_deploy:,.0f} = "
            f"₹{sector_deployed + capital_to_deploy:,.0f} / ₹{sector_cap:,.0f} cap."
        ),
    )
