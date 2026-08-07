"""
Regime classifier — the Researcher's first job each cycle.

Classifies market state into 5 buckets using DETERMINISTIC Python math on
real Kite Connect market data. NOT an LLM call — must be reproducible,
auditable, and fast enough to run every hour.

Inputs (all fetched from Kite Connect each cycle):
  1. Nifty 50 LTP vs its 200-day EMA         — trend direction
  2. India VIX level and 5-day trend          — fear/greed sentiment
  3. % of universe tickers above their 50-SMA — market breadth

Regime inertia: requires REGIME_INERTIA_PERIODS consecutive matching reads
before the regime bucket can change. Prevents "strategy whiplash" at noisy
regime transitions (bull→sideways→bull oscillation).
"""

import logging
import os
from dataclasses import dataclass
from datetime import date, timedelta
from enum import Enum
from typing import Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Kite instrument tokens — stable NSE identifiers for index instruments.
# If these ever need refreshing: kite.instruments("NSE") and grep for the name.
# ---------------------------------------------------------------------------
NIFTY50_TOKEN = 256265      # NSE:NIFTY 50
INDIA_VIX_TOKEN = 264969    # NSE:INDIA VIX

# ---------------------------------------------------------------------------
# Regime thresholds
# ---------------------------------------------------------------------------
VIX_CALM = 15.0         # below this → calm / bullish sentiment
VIX_ELEVATED = 20.0     # above this → mild fear
VIX_HIGH_FEAR = 25.0    # above this → high fear
VIX_EXTREME = 30.0      # above this → extreme fear / potential crash

NIFTY_EUPHORIA_BAND_PCT = 8.0     # Nifty > 200-EMA by this much → euphoria zone
NIFTY_BULL_BAND_PCT = 2.0         # Nifty > 200-EMA by this much → bull
NIFTY_BEAR_BAND_PCT = -3.0        # Nifty < 200-EMA by this much → bear
NIFTY_CRASH_BAND_PCT = -12.0      # Nifty < 200-EMA by this much → crash

BREADTH_BULL = 65       # % universe above 50-SMA that signals broad bull
BREADTH_BEAR = 40       # % universe above 50-SMA that signals weakness

# Inertia: how many consecutive identical readings before switching regime
REGIME_INERTIA_PERIODS = int(os.getenv("REGIME_INERTIA_PERIODS", "2"))

# ---------------------------------------------------------------------------
# Module-level inertia state (in-process; reset on deploy — acceptable)
# ---------------------------------------------------------------------------
_regime_history: list = []


class Regime(str, Enum):
    CRASH = "extreme_fear_crash"
    BEAR = "bear"
    SIDEWAYS = "sideways"
    BULL = "bull"
    EUPHORIA = "euphoria"


@dataclass
class RegimeReading:
    regime: Regime
    confidence: float        # 0–100
    rationale: str
    raw_score: float         # composite score (for audit trail)
    nifty_vs_ema_pct: float  # % Nifty 50 is above/below its 200-EMA
    vix: float               # India VIX current value
    breadth_pct: float       # % universe stocks above their 50-SMA


# ---------------------------------------------------------------------------
# Technical helpers (pure math — no external calls)
# ---------------------------------------------------------------------------

def _ema(prices: list[float], period: int) -> float:
    """Exponential moving average of the last value in prices."""
    if len(prices) < period:
        return sum(prices) / len(prices)
    k = 2.0 / (period + 1)
    ema = sum(prices[:period]) / period   # seed with SMA
    for price in prices[period:]:
        ema = price * k + ema * (1 - k)
    return ema


def _sma(prices: list[float], period: int) -> float:
    if len(prices) < period:
        return sum(prices) / len(prices)
    return sum(prices[-period:]) / period


# ---------------------------------------------------------------------------
# Data fetchers (each returns a safe fallback on error)
# ---------------------------------------------------------------------------

def _fetch_nifty_vs_200ema(kite) -> tuple[float, float]:
    """
    Returns (nifty_ltp, pct_vs_200ema).
    Fetches ~14 months of daily history to compute the 200-day EMA cleanly.
    """
    from_date = (date.today() - timedelta(days=420)).strftime("%Y-%m-%d")
    to_date = date.today().strftime("%Y-%m-%d")

    try:
        hist = kite.historical_data(
            NIFTY50_TOKEN, from_date, to_date, "day",
            continuous=False, oi=False,
        )
        closes = [c["close"] for c in hist]
        if len(closes) < 50:
            raise ValueError(f"Too few Nifty history bars: {len(closes)}")
        ltp = closes[-1]
        ema200 = _ema(closes, min(200, len(closes)))
        pct = (ltp - ema200) / ema200 * 100
        log.debug(f"Nifty 50: ₹{ltp:,.0f} | 200-EMA: ₹{ema200:,.0f} | Δ: {pct:+.1f}%")
        return ltp, round(pct, 2)
    except Exception as e:
        log.error(f"Nifty 50 history fetch failed, falling back to LTP only: {e}")
        try:
            q = kite.ltp(["NSE:NIFTY 50"])
            ltp = q["NSE:NIFTY 50"]["last_price"]
            return ltp, 0.0   # assume neutral vs EMA
        except Exception as e2:
            log.error(f"Nifty 50 LTP fetch also failed: {e2}")
            return 22000.0, 0.0   # hardcoded fallback — will produce SIDEWAYS


def _fetch_vix(kite) -> tuple[float, float]:
    """
    Returns (vix_current, vix_5d_change_pct).
    Rising VIX = increasing fear; falling = easing.
    """
    from_date = (date.today() - timedelta(days=40)).strftime("%Y-%m-%d")
    to_date = date.today().strftime("%Y-%m-%d")

    try:
        hist = kite.historical_data(INDIA_VIX_TOKEN, from_date, to_date, "day")
        closes = [c["close"] for c in hist]
        if not closes:
            raise ValueError("Empty VIX history")
        vix = closes[-1]
        vix_5d_ago = closes[-6] if len(closes) >= 6 else closes[0]
        change = (vix - vix_5d_ago) / vix_5d_ago * 100
        log.debug(f"India VIX: {vix:.1f} | 5d change: {change:+.1f}%")
        return vix, round(change, 1)
    except Exception as e:
        log.error(f"VIX history fetch failed, trying LTP: {e}")
        try:
            q = kite.ltp(["NSE:INDIA VIX"])
            vix = q["NSE:INDIA VIX"]["last_price"]
            return vix, 0.0
        except Exception as e2:
            log.error(f"VIX LTP fetch also failed: {e2}")
            return 18.0, 0.0   # assume normal


def _fetch_breadth(kite, tickers: list[str], instruments_map: dict) -> float:
    """
    % of sampled universe tickers where LTP > 50-day SMA.
    Samples up to 20 tickers (enough for breadth signal; spares API quota).
    Returns 50.0 (neutral) on complete failure.
    """
    sample = tickers[:20]
    from_date = (date.today() - timedelta(days=100)).strftime("%Y-%m-%d")
    to_date = date.today().strftime("%Y-%m-%d")

    bullish = 0
    total = 0

    for ticker in sample:
        token = instruments_map.get(ticker)
        if not token:
            continue
        try:
            hist = kite.historical_data(int(token), from_date, to_date, "day")
            closes = [c["close"] for c in hist]
            if len(closes) < 10:
                continue
            ltp = closes[-1]
            sma50 = _sma(closes, min(50, len(closes)))
            if ltp > sma50:
                bullish += 1
            total += 1
        except Exception:
            continue

    if total == 0:
        log.warning("Could not compute breadth — returning neutral 50%")
        return 50.0
    breadth = bullish / total * 100
    log.debug(f"Breadth: {bullish}/{total} tickers above 50-SMA = {breadth:.0f}%")
    return round(breadth, 1)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _score_components(
    nifty_pct: float, vix: float, vix_5d_change: float, breadth_pct: float
) -> tuple[float, list[str]]:
    """
    Returns (composite_score, rationale_parts).
    Score range roughly -10 to +8; calibrated for 5-bucket mapping below.
    """
    score = 0.0
    parts = []

    # 1. Nifty vs 200-EMA (most important structural signal, weight ≈ 3)
    if nifty_pct >= NIFTY_EUPHORIA_BAND_PCT:
        score += 3.0
        parts.append(f"Nifty {nifty_pct:+.1f}% vs 200-EMA (euphoria zone)")
    elif nifty_pct >= NIFTY_BULL_BAND_PCT:
        score += 2.0
        parts.append(f"Nifty {nifty_pct:+.1f}% vs 200-EMA (bullish)")
    elif nifty_pct >= 0:
        score += 0.5
        parts.append(f"Nifty {nifty_pct:+.1f}% vs 200-EMA (slight edge)")
    elif nifty_pct >= NIFTY_BEAR_BAND_PCT:
        score -= 1.0
        parts.append(f"Nifty {nifty_pct:+.1f}% vs 200-EMA (mildly bearish)")
    elif nifty_pct >= NIFTY_CRASH_BAND_PCT:
        score -= 3.0
        parts.append(f"Nifty {nifty_pct:+.1f}% vs 200-EMA (bear territory)")
    else:
        score -= 5.0
        parts.append(f"Nifty {nifty_pct:+.1f}% vs 200-EMA (CRASH territory)")

    # 2. VIX level (fear/greed, weight ≈ 2.5)
    if vix < VIX_CALM:
        score += 2.0
        parts.append(f"VIX {vix:.1f} (calm, no fear)")
    elif vix < VIX_ELEVATED:
        score += 0.5
        parts.append(f"VIX {vix:.1f} (normal)")
    elif vix < VIX_HIGH_FEAR:
        score -= 1.5
        parts.append(f"VIX {vix:.1f} (elevated fear)")
    elif vix < VIX_EXTREME:
        score -= 3.0
        parts.append(f"VIX {vix:.1f} (high fear)")
    else:
        score -= 5.0
        parts.append(f"VIX {vix:.1f} (EXTREME fear)")

    # 3. VIX momentum (punish rising fear, reward easing)
    if vix_5d_change < -10:
        score += 1.0
        parts.append(f"VIX falling sharply ({vix_5d_change:+.0f}% 5d — fear easing)")
    elif vix_5d_change > 20:
        score -= 1.0
        parts.append(f"VIX rising sharply ({vix_5d_change:+.0f}% 5d — fear building)")

    # 4. Market breadth (width of participation, weight ≈ 2)
    if breadth_pct >= BREADTH_BULL:
        score += 2.0
        parts.append(f"Breadth {breadth_pct:.0f}% above 50-SMA (broad bull)")
    elif breadth_pct >= 50:
        score += 0.5
        parts.append(f"Breadth {breadth_pct:.0f}% above 50-SMA (mixed/mild)")
    elif breadth_pct >= BREADTH_BEAR:
        score -= 1.0
        parts.append(f"Breadth {breadth_pct:.0f}% above 50-SMA (weak breadth)")
    else:
        score -= 2.0
        parts.append(f"Breadth {breadth_pct:.0f}% above 50-SMA (poor breadth)")

    return score, parts


def _map_score_to_regime(
    score: float, nifty_pct: float, vix: float
) -> tuple[Regime, float]:
    """
    Maps composite score → regime bucket + confidence.
    Hard overrides apply for extreme single-signal readings
    (VIX > 30 or Nifty > 12% below 200-EMA = Crash regardless of score).
    """
    # Hard overrides — extreme signals override composite score
    if vix >= VIX_EXTREME or nifty_pct <= NIFTY_CRASH_BAND_PCT:
        return Regime.CRASH, min(90.0, 70 + abs(min(vix - VIX_EXTREME, 0)) + abs(min(nifty_pct - NIFTY_CRASH_BAND_PCT, 0)))

    # Score-based mapping.
    #
    # Euphoria: requires high score AND Nifty visibly extended above EMA.
    #   Low VIX + good breadth alone = great bull market, not euphoria.
    #
    # Crash: requires high negative score AND (VIX spiking OR Nifty deeply below EMA).
    #   Weak breadth + elevated VIX + modest Nifty underperformance = bear, not crash.
    #   Boundaries: SIDEWAYS [-2, +2), BULL [+2, euphoria), BEAR [-6, -2), CRASH < -6
    if score >= 5.5 and nifty_pct >= NIFTY_EUPHORIA_BAND_PCT:
        regime, base_conf = Regime.EUPHORIA, 70.0
    elif score >= 2.0:
        regime, base_conf = Regime.BULL, 65.0
    elif score >= -2.0:
        regime, base_conf = Regime.SIDEWAYS, 60.0
    elif score >= -6.0 or (vix < VIX_HIGH_FEAR and nifty_pct > NIFTY_CRASH_BAND_PCT):
        # A score below -6 normally → CRASH, but NOT if VIX is still manageable
        # and Nifty hasn't collapsed. In that case, stay in BEAR.
        regime, base_conf = Regime.BEAR, 65.0
    else:
        regime, base_conf = Regime.CRASH, 70.0

    # Confidence scales with distance from bucket boundary
    if regime == Regime.BULL:
        confidence = min(90.0, base_conf + (score - 2.0) * 6)
    elif regime == Regime.EUPHORIA:
        confidence = min(92.0, base_conf + (score - 5.5) * 5)
    elif regime == Regime.SIDEWAYS:
        confidence = min(80.0, base_conf + (1.0 - abs(score)) * 8)
    elif regime == Regime.BEAR:
        confidence = min(88.0, base_conf + (abs(score) - 1.0) * 6)
    else:  # CRASH
        confidence = min(90.0, base_conf + abs(score + 4.0) * 5)

    return regime, max(40.0, confidence)


# ---------------------------------------------------------------------------
# Main public function
# ---------------------------------------------------------------------------

def classify_regime() -> RegimeReading:
    """
    Fetches real Kite Connect market data and classifies the current regime.
    This is deterministic Python math — no LLM.

    Requires KITE_API_KEY + KITE_ACCESS_TOKEN in .env and a valid daily session.
    Run setup/refresh_kite_token.py each morning before 9:15 AM.
    """
    global _regime_history

    from trader.kite_client import get_kite_client
    from universe.loader import get_tickers

    kite = get_kite_client()
    tickers = get_tickers()

    # One instruments list fetch, shared for breadth computation
    try:
        instruments_map = {
            row["tradingsymbol"]: row["instrument_token"]
            for row in kite.instruments("NSE")
            if row["instrument_type"] == "EQ"
        }
    except Exception as e:
        log.error(f"Instruments list fetch failed: {e}")
        instruments_map = {}

    # Fetch all market data
    nifty_ltp, nifty_vs_ema = _fetch_nifty_vs_200ema(kite)
    vix, vix_5d_change = _fetch_vix(kite)
    breadth_pct = _fetch_breadth(kite, tickers, instruments_map)

    log.info(
        f"Regime inputs — Nifty: ₹{nifty_ltp:,.0f} ({nifty_vs_ema:+.1f}% vs 200-EMA) | "
        f"VIX: {vix:.1f} (5d: {vix_5d_change:+.0f}%) | Breadth: {breadth_pct:.0f}%"
    )

    # Compute composite score
    score, rationale_parts = _score_components(nifty_vs_ema, vix, vix_5d_change, breadth_pct)
    raw_regime, raw_confidence = _map_score_to_regime(score, nifty_vs_ema, vix)

    # ---------------------------------------------------------------------------
    # Inertia smoothing — require N consecutive identical readings before switching
    # ---------------------------------------------------------------------------
    _regime_history.append(raw_regime)
    _regime_history = _regime_history[-REGIME_INERTIA_PERIODS:]  # keep only recent N

    confirmed_regime = raw_regime
    confidence = raw_confidence

    if len(_regime_history) >= 2:
        prev = _regime_history[-2]
        if prev != raw_regime:
            # Transition detected — hold previous regime, drop confidence
            confirmed_regime = prev
            confidence = max(40.0, raw_confidence - 25.0)
            rationale_parts.append(
                f"⚠ Transition {prev.value} → {raw_regime.value} detected. "
                f"Holding {prev.value} until confirmed next cycle."
            )

    rationale = " | ".join(rationale_parts)
    log.info(
        f"Regime: {confirmed_regime.value.upper()} | "
        f"confidence: {confidence:.0f}% | score: {score:+.1f}"
    )

    return RegimeReading(
        regime=confirmed_regime,
        confidence=confidence,
        rationale=rationale,
        raw_score=score,
        nifty_vs_ema_pct=nifty_vs_ema,
        vix=vix,
        breadth_pct=breadth_pct,
    )
