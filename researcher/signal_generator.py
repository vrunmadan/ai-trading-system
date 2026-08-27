"""
Signal generator — given a regime reading, scans the universe CSV and
returns the single highest-conviction trade signal above threshold, or None.

Architecture (separation of concerns):
  Python (this file) does all data fetching and number-crunching:
    - Kite Connect historical OHLCV for each ticker
    - RSI, Supertrend, volume ratio, 52-week proximity, Bollinger position
  Claude Sonnet 5 does synthesis:
    - Receives pre-computed indicators as structured text
    - Applies the strategy basket criteria
    - Scores technical fit and fundamental judgment
    - Returns a strict JSON verdict

Claude does NOT make API calls live — we pass computed data as context.
This keeps the model's job pure synthesis, avoids rate limits, and makes
every signal fully auditable (the raw indicators are logged in the rationale).
"""

import json
import logging
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

import anthropic
import pytz

from researcher.regime_classifier import Regime, RegimeReading
from universe.loader import get_tickers, load_universe

log = logging.getLogger(__name__)

RESEARCHER_MODEL = os.getenv("RESEARCHER_MODEL", "claude-sonnet-5")
MIN_CONFIDENCE = float(os.getenv("MIN_SIGNAL_CONFIDENCE", "75"))
IST = pytz.timezone("Asia/Kolkata")


@dataclass
class TradeSignal:
    ticker: str
    sector: str            # from universe CSV (for Risk Sizer sector check)
    exchange: str          # "NSE" or "BSE" — which exchange to route order to
    regime: Regime
    strategy_bucket: str
    direction: str         # "BUY" (system only does longs currently)
    technical_score: float  # 0-100
    fundamental_score: float  # 0-100
    confidence_score: float   # 0-100 weighted composite
    rationale: str


# ---------------------------------------------------------------------------
# Strategy baskets — one per regime bucket.
# Each strategy has a name + plain-English entry criteria passed to Claude.
#
# EVIDENCE-BASED MAPPING (10y backtest, Nifty100/Midcap150, 20% trailing exit —
# see streak_backtests/backtest.py --by-regime). Key findings that shaped this:
#   • Trend/breakout family (52wk_breakout, bb_squeeze_break, adx_bull_strength)
#     is the BULL/EUPHORIA engine: PF ~2.4–3.4, drawdowns ~-20 to -28%.
#   • supertrend_buy was REMOVED from BULL: weakest trend strategy there
#     (PF ~1.5–1.8) with a -48% to -60% drawdown — it whipsaws in strong trends.
#   • Mean-reversion (rsi/bb/cci) is the WEAK LINK. On the FULL Nifty-500 universe
#     WITH 50bps costs it posts PF 1.6–2.1 in sideways with -82% to -91% drawdowns
#     (knife-catching on smaller-caps) — beaten by the breakout family in EVERY
#     long-tradable regime, including sideways. So it was REMOVED from all baskets.
#   • SIDEWAYS is breakout-led too: golden_cross_ema (PF 3.74), 52wk_breakout and
#     bb_squeeze_break all beat mean-reversion with ~1/3 the drawdown. Breakouts
#     from a consolidation catch the next trend leg.
#   • BEAR/CRASH stay empty: the backtest's "edge" there was hold-into-recovery
#     bias, not tradable. Long-only ⇒ capital preservation (cash) is the edge.
# Net: ONE coherent engine (breakouts + 20% trailing exit); the regime only
# decides ACTIVE (bull/euphoria/sideways) vs CASH (bear/crash).
# Add new strategies only after a --by-regime backtest shows PF>1.3 AND positive
# expectancy in the target regime.
# ---------------------------------------------------------------------------
_S_52WK_BREAKOUT = {
    "name": "52wk_breakout",
    "description": (
        "Entry: LTP has just broken above OR is within 1% of the 52-week high. "
        "Volume today is ≥ 1.5x the 20-day average (breakout confirmation). "
        "RSI(14) is 50–70 (momentum but not overbought). Supertrend(10,3) is GREEN. "
        "Thesis: momentum continuation after a proven resistance level is cleared "
        "with volume. Do NOT fire on a high-RSI stock near its 52-week high if "
        "volume is below average — that's a grind, not a breakout."
    ),
}
_S_BB_SQUEEZE_BREAK = {
    "name": "bb_squeeze_break",
    "description": (
        "Entry: the stock was in a Bollinger-Band SQUEEZE (bb_squeeze = True: "
        "bandwidth in the bottom 20% of the last 100 days — a volatility contraction) "
        "AND price is now breaking ABOVE the upper band (bb_breaking_upper = True) "
        "with RSI(14) > 50. Thesis: volatility contraction resolving upward into a new "
        "expansion leg. Do NOT fire if there was no prior squeeze — a band break without "
        "a preceding squeeze is just chasing."
    ),
}
_S_ADX_BULL_STRENGTH = {
    "name": "adx_bull_strength",
    "description": (
        "Entry: ADX(14) has just crossed ABOVE 25 (trend strength emerging), +DI is above "
        "-DI (bulls in control), and LTP is above the 50-day SMA. Thesis: a fresh, "
        "strengthening uptrend. Do NOT fire if ADX is high but FALLING, or if -DI > +DI "
        "(that's a strengthening DOWNtrend)."
    ),
}
_S_GOLDEN_CROSS = {
    "name": "golden_cross_ema",
    "description": (
        "Entry: the 50-day EMA has just crossed ABOVE the 200-day EMA (a fresh golden "
        "cross, ema50_above_ema200 = True with golden_cross_recent = True), with LTP above "
        "both. Thesis: a durable trend-regime change confirming from a base/consolidation — "
        "the strongest sideways-to-uptrend transition in the backtest. Do NOT fire on a "
        "long-established cross (that's chasing) or when price is far extended above the EMAs."
    ),
}
_S_RSI_MEAN_REVERSION = {
    "name": "rsi_mean_reversion",
    "description": (
        "Entry: daily RSI(14) < 35 AND Bollinger position < 25% (near lower band). "
        "Stock is NOT making new 52-week lows (avoid falling knives). "
        "Thesis: oversold bounce in a range-bound market. Do NOT fire if RSI is "
        "declining fast (still in freefall) — wait for RSI to turn up before entry."
    ),
}
_S_BB_MEAN_REVERSION = {
    "name": "bb_mean_reversion",
    "description": (
        "Entry: LTP has closed BELOW the lower Bollinger band AND RSI(14) < 40, and the "
        "stock is NOT making a new 52-week low. Thesis: stretched-to-the-downside snapback "
        "inside a range. Do NOT fire on a stock in a clean downtrend (below a falling "
        "50-day SMA with -DI dominant) — that's a knife."
    ),
}
_S_CCI_RECOVERY = {
    "name": "cci_recovery",
    "description": (
        "Entry: CCI(20) has just crossed back ABOVE -100 from below (oversold momentum "
        "turning up), and the stock is NOT making a new 52-week low. Thesis: mean-reversion "
        "with a momentum-turn confirmation, for a range-bound market. Do NOT fire while CCI "
        "is still falling below -100."
    ),
}

STRATEGY_BASKETS: dict[Regime, list[dict]] = {
    # Trend/breakout engine. supertrend_buy intentionally excluded (see notes above).
    Regime.BULL: [_S_52WK_BREAKOUT, _S_BB_SQUEEZE_BREAK, _S_ADX_BULL_STRENGTH],
    # Late-cycle: same breakouts still print (PF 2.7–4.0) but froth risk is high —
    # the Risk Sizer should size these smaller. Revises the earlier "no euphoria
    # entries" stance based on backtest evidence.
    Regime.EUPHORIA: [_S_ADX_BULL_STRENGTH, _S_BB_SQUEEZE_BREAK, _S_52WK_BREAKOUT],
    # Range: breakout-led (consolidation breakouts catch the next trend leg).
    # Mean-reversion removed — worse PF and 2–3x the drawdown on the full universe.
    Regime.SIDEWAYS: [_S_52WK_BREAKOUT, _S_BB_SQUEEZE_BREAK, _S_GOLDEN_CROSS],
    Regime.BEAR: [],      # No new longs — capital preservation (cash).
    Regime.CRASH: [],     # No new longs — capital preservation (cash).
}


# ---------------------------------------------------------------------------
# Technical indicator helpers (pure Python — no external calls)
# ---------------------------------------------------------------------------

def _rsi(closes: list[float], period: int = 14) -> float:
    """Wilder's RSI."""
    if len(closes) < period + 1:
        return 50.0
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    avg_gain = sum(max(d, 0) for d in deltas[:period]) / period
    avg_loss = sum(max(-d, 0) for d in deltas[:period]) / period
    for d in deltas[period:]:
        avg_gain = (avg_gain * (period - 1) + max(d, 0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-d, 0)) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 1)


def _supertrend(highs: list[float], lows: list[float], closes: list[float],
                period: int = 10, multiplier: float = 3.0) -> tuple[str, bool]:
    """
    Returns (current_direction, just_flipped).
    direction: 'GREEN' (bullish) or 'RED' (bearish).
    just_flipped: True if direction changed in the last 2 bars.
    """
    if len(closes) < period + 3:
        return "UNKNOWN", False

    # Compute ATR using Wilder's smoothing
    tr_list = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        tr_list.append(tr)

    atr_list = [sum(tr_list[:period]) / period]
    for tr in tr_list[period:]:
        atr_list.append((atr_list[-1] * (period - 1) + tr) / period)

    # Supertrend bands (indexed against closes[period:])
    n = len(atr_list)
    directions = []
    prev_dir = "GREEN"
    prev_upper = None
    prev_lower = None

    for i in range(n):
        idx = i + period  # index into closes/highs/lows
        mid = (highs[idx] + lows[idx]) / 2
        upper = mid + multiplier * atr_list[i]
        lower = mid - multiplier * atr_list[i]

        if prev_upper is not None:
            upper = min(upper, prev_upper) if closes[idx - 1] < prev_upper else upper
            lower = max(lower, prev_lower) if closes[idx - 1] > prev_lower else lower

        close = closes[idx]
        if prev_dir == "RED" and close > upper:
            cur_dir = "GREEN"
        elif prev_dir == "GREEN" and close < lower:
            cur_dir = "RED"
        else:
            cur_dir = prev_dir

        directions.append(cur_dir)
        prev_dir = cur_dir
        prev_upper = upper
        prev_lower = lower

    current = directions[-1]
    just_flipped = len(directions) >= 2 and directions[-1] != directions[-2]
    return current, just_flipped


def _volume_ratio(volumes: list[float]) -> float:
    """Today's volume / 20-day average (using the 20 days before today)."""
    if len(volumes) < 2:
        return 1.0
    today = volumes[-1]
    window = volumes[-21:-1]
    avg = sum(window) / len(window) if window else today
    return round(today / avg, 2) if avg > 0 else 1.0


def _pct_from_52wk_high(ltp: float, closes: list[float]) -> float:
    """% distance from 52-week high. Negative means below."""
    lookback = closes[-252:] if len(closes) >= 252 else closes
    high = max(lookback)
    return round((ltp - high) / high * 100, 1)


def _bollinger_position(closes: list[float], period: int = 20, mult: float = 2.0) -> float:
    """
    Position within Bollinger Band: 0 = lower band, 100 = upper band.
    < 25 signals near lower band (potential mean-reversion buy).
    """
    if len(closes) < period:
        return 50.0
    recent = closes[-period:]
    sma = sum(recent) / period
    std = (sum((x - sma) ** 2 for x in recent) / period) ** 0.5
    if std == 0:
        return 50.0
    upper = sma + mult * std
    lower = sma - mult * std
    return round((closes[-1] - lower) / (upper - lower) * 100, 1)


def _ema_series(closes: list[float], period: int) -> list[float]:
    """Full EMA series aligned to closes."""
    if not closes:
        return []
    k = 2.0 / (period + 1)
    out = [closes[0]]
    ema = closes[0]
    for c in closes[1:]:
        ema = c * k + ema * (1 - k)
        out.append(ema)
    return out


def _golden_cross(closes: list[float]) -> tuple[bool, bool]:
    """Returns (ema50_above_ema200, just_crossed_up_within_2_bars)."""
    if len(closes) < 205:
        return False, False
    e50 = _ema_series(closes, 50)
    e200 = _ema_series(closes, 200)
    above = e50[-1] > e200[-1]
    crossed = any(e50[-1 - k] > e200[-1 - k] and e50[-2 - k] <= e200[-2 - k] for k in range(2))
    return above, crossed


def _adx(highs: list[float], lows: list[float], closes: list[float],
         period: int = 14) -> tuple[float, float, float]:
    """Returns (ADX, +DI, -DI) — Wilder. All 0.0 if insufficient history."""
    n = len(closes)
    if n < 2 * period + 2:
        return 0.0, 0.0, 0.0
    tr = [0.0] * n; pdm = [0.0] * n; mdm = [0.0] * n
    for i in range(1, n):
        up = highs[i] - highs[i - 1]; dn = lows[i - 1] - lows[i]
        pdm[i] = up if (up > dn and up > 0) else 0.0
        mdm[i] = dn if (dn > up and dn > 0) else 0.0
        tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
    atr = sum(tr[1:period + 1]); sp = sum(pdm[1:period + 1]); sm = sum(mdm[1:period + 1])
    pdi = mdi = 0.0; dxs = []
    for i in range(period + 1, n):
        atr = atr - atr / period + tr[i]
        sp = sp - sp / period + pdm[i]
        sm = sm - sm / period + mdm[i]
        pdi = 100 * sp / atr if atr else 0.0
        mdi = 100 * sm / atr if atr else 0.0
        denom = pdi + mdi
        dxs.append(100 * abs(pdi - mdi) / denom if denom else 0.0)
    if len(dxs) < period:
        return 0.0, round(pdi, 1), round(mdi, 1)
    adx = sum(dxs[:period]) / period
    for dx in dxs[period:]:
        adx = (adx * (period - 1) + dx) / period
    return round(adx, 1), round(pdi, 1), round(mdi, 1)


def _cci(highs: list[float], lows: list[float], closes: list[float], period: int = 20) -> float:
    """Commodity Channel Index (current value)."""
    if len(closes) < period:
        return 0.0
    tp = [(highs[i] + lows[i] + closes[i]) / 3 for i in range(len(closes))]
    window = tp[-period:]
    sma = sum(window) / period
    md = sum(abs(x - sma) for x in window) / period
    return round((tp[-1] - sma) / (0.015 * md), 1) if md else 0.0


def _bb_bandwidth_squeeze(closes: list[float], period: int = 20, mult: float = 2.0,
                          lookback: int = 100) -> tuple[float, bool, bool]:
    """Returns (bandwidth_pct, is_squeeze, breaking_above_upper).
    is_squeeze: current bandwidth in the bottom 20% of the last `lookback` bars.
    breaking_above_upper: close just crossed above the upper band this bar."""
    n = len(closes)
    if n < period + 2:
        return 0.0, False, False

    def band(i):
        w = closes[i - period + 1:i + 1]
        m = sum(w) / period
        std = (sum((x - m) ** 2 for x in w) / period) ** 0.5
        return m + mult * std, m - mult * std, ((2 * mult * std) / m * 100 if m else 0.0)

    up_now, _, bw_now = band(n - 1)
    up_prev, _, _ = band(n - 2)
    breaking = closes[-1] > up_now and closes[-2] <= up_prev
    hist_bw = [band(i)[2] for i in range(max(period - 1, n - lookback), n)]
    thresh = sorted(hist_bw)[int(0.20 * len(hist_bw))] if hist_bw else 0.0
    is_squeeze = bw_now <= thresh and bw_now > 0
    return round(bw_now, 2), is_squeeze, breaking


def _compute_indicators(hist: list[dict], ticker: str) -> dict:
    """Compute all technical indicators from Kite historical_data bars."""
    closes = [c["close"] for c in hist]
    highs = [c["high"] for c in hist]
    lows = [c["low"] for c in hist]
    volumes = [c["volume"] for c in hist]
    ltp = closes[-1]

    st_dir, st_flipped = _supertrend(highs, lows, closes)
    n50 = min(50, len(closes))
    n200 = min(200, len(closes))
    adx, plus_di, minus_di = _adx(highs, lows, closes)
    ema50_above_200, golden_cross = _golden_cross(closes)
    bb_bw, bb_squeeze, bb_breakout = _bb_bandwidth_squeeze(closes)

    return {
        "ticker": ticker,
        "ltp": ltp,
        "rsi_14": _rsi(closes),
        "supertrend_10_3": st_dir,
        "supertrend_just_flipped": st_flipped,
        "volume_ratio_20d": _volume_ratio(volumes),
        "pct_from_52wk_high": _pct_from_52wk_high(ltp, closes),
        "bollinger_position": _bollinger_position(closes),
        "sma_50": round(sum(closes[-n50:]) / n50, 2),
        "sma_200": round(sum(closes[-n200:]) / n200, 2),
        "above_sma50": ltp > sum(closes[-n50:]) / n50,
        "above_sma200": ltp > sum(closes[-n200:]) / n200,
        "atr_pct": round((max(highs[-14:]) - min(lows[-14:])) / ltp / 14 * 100, 2),
        # --- indicators added for the evidence-based strategy baskets ---
        "adx_14": adx,
        "plus_di": plus_di,
        "minus_di": minus_di,
        "cci_20": _cci(highs, lows, closes),
        "ema50_above_ema200": ema50_above_200,
        "golden_cross_recent": golden_cross,
        "bb_bandwidth_pct": bb_bw,
        "bb_squeeze": bb_squeeze,
        "bb_breaking_upper": bb_breakout,
    }


# ---------------------------------------------------------------------------
# Deterministic pre-filter — the cheap gate that runs BEFORE any Claude call
# (and before the per-ticker news fetch). Only tickers that pass a strategy's
# hard technical rules are sent to Claude. This is what makes a 500-name
# universe affordable: it collapses ~1,500 Claude calls/cycle to a few dozen.
#
# Mirrors the entry rules validated in streak_backtests/backtest.py, but is
# intentionally PERMISSIVE — it rejects only obvious non-candidates and leaves
# the fine judgment (freshness of a cross, "not a falling knife", news) to
# Claude. Any strategy without an explicit rule FAILS OPEN (returns True), so a
# new basket entry is never silently dropped.
# ---------------------------------------------------------------------------

def _passes_prefilter(strategy_name: str, ind: dict) -> bool:
    rsi = ind.get("rsi_14", 50.0)
    st = ind.get("supertrend_10_3", "UNKNOWN")
    vol = ind.get("volume_ratio_20d", 0.0)
    from_high = ind.get("pct_from_52wk_high", -100.0)
    bb_pos = ind.get("bollinger_position", 50.0)
    adx = ind.get("adx_14", 0.0)
    pdi = ind.get("plus_di", 0.0)
    mdi = ind.get("minus_di", 0.0)
    cci = ind.get("cci_20", 0.0)
    above_sma50 = ind.get("above_sma50", False)

    if strategy_name == "52wk_breakout":
        return from_high >= -1.5 and vol >= 1.5 and 50 <= rsi <= 70 and st == "GREEN"
    if strategy_name == "bb_squeeze_break":
        return bool(ind.get("bb_squeeze")) and bool(ind.get("bb_breaking_upper")) and rsi > 50
    if strategy_name == "adx_bull_strength":
        return adx > 25 and pdi > mdi and above_sma50
    if strategy_name == "golden_cross_ema":
        return bool(ind.get("ema50_above_ema200")) and bool(ind.get("golden_cross_recent"))
    if strategy_name == "cci_recovery":
        # recovering out of oversold: back above -100 but still in the low zone
        return -100 < cci < -20
    if strategy_name == "bb_mean_reversion":
        return bb_pos < 20 and rsi < 40
    if strategy_name == "rsi_mean_reversion":
        return rsi < 35 and bb_pos < 25
    return True  # fail-open: unknown strategy still reaches Claude


# ---------------------------------------------------------------------------
# Qualitative context — Google News RSS (no API key, standard library only)
# ---------------------------------------------------------------------------

def _fetch_qualitative_context(ticker: str, sector: str, company_name: str = "") -> str:
    """
    Fetch recent news headlines from Google News RSS for:
      1. Broad Indian market / macro
      2. The stock's sector
      3. The specific company

    Returns a plain-text block for injection into the Claude prompt.
    Fails gracefully — any network/parse error returns a fallback string
    so the signal cycle is never blocked by a news fetch failure.
    """
    import urllib.request
    import urllib.parse
    import xml.etree.ElementTree as ET
    from datetime import datetime, timedelta, timezone
    from email.utils import parsedate_to_datetime

    cutoff = datetime.now(timezone.utc) - timedelta(days=7)

    def _headlines(query: str, max_items: int = 5) -> list[str]:
        url = (
            "https://news.google.com/rss/search?"
            + urllib.parse.urlencode(
                {"q": query, "hl": "en-IN", "gl": "IN", "ceid": "IN:en"}
            )
        )
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                tree = ET.parse(resp)
        except Exception as e:
            log.debug(f"News RSS fetch failed for '{query}': {e}")
            return []

        results = []
        for item in tree.findall(".//item"):
            title_el = item.find("title")
            pub_el = item.find("pubDate")
            if title_el is None:
                continue
            title = (title_el.text or "").strip()
            if pub_el is not None and pub_el.text:
                try:
                    pub_dt = parsedate_to_datetime(pub_el.text)
                    if pub_dt < cutoff:
                        continue
                except Exception:
                    pass
            if title:
                results.append(title)
            if len(results) >= max_items:
                break
        return results

    name = company_name or ticker
    macro_heads   = _headlines("Nifty 50 India market outlook")
    sector_heads  = _headlines(f"{sector} India stocks")
    company_heads = _headlines(f"{name} NSE")

    sections = []
    if macro_heads:
        sections.append(
            "MACRO/MARKET NEWS (last 7 days):\n"
            + "\n".join(f"  • {h}" for h in macro_heads)
        )
    if sector_heads:
        sections.append(
            f"SECTOR NEWS ({sector}, last 7 days):\n"
            + "\n".join(f"  • {h}" for h in sector_heads)
        )
    if company_heads:
        sections.append(
            f"COMPANY NEWS ({name}, last 7 days):\n"
            + "\n".join(f"  • {h}" for h in company_heads)
        )

    if not sections:
        return "(No recent news fetched — evaluate fundamental_score on technicals and sector judgment only.)"

    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Claude Sonnet 5 synthesis
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """You are a quantitative analyst for a disciplined AI trading system
focused on Indian equities. A Python pipeline has already fetched and computed all
technical indicators from real market data. Your job is to synthesize a verdict.

RULES — these are non-negotiable:
1. Reject far more often than you approve. 75%+ confidence means genuinely exceptional —
   not "slightly interesting." Ask yourself: "Would I stake 15–20% of the portfolio on
   this today?" If hesitant, score lower and return PASS.
2. Technical score must follow from the numbers provided. If volume_ratio is 1.1x
   and the strategy requires ≥ 1.5x, that's a technical_score ≤ 50.
3. confidence_score = 0.6 × technical_score + 0.4 × fundamental_score. Compute and
   report it accurately; do not round up.
4. If confidence_score < 75, return "PASS" — do not signal.

QUALITATIVE WEIGHING (for fundamental_score):
Start fundamental_score at 60 (neutral), then adjust:
  • Company-specific NEGATIVE news (earnings miss, promoter selling, regulatory action,
    fraud allegations, plant shutdown, major debt downgrade): subtract 20–40 points.
  • Company-specific POSITIVE news (strong earnings beat, order win, capacity expansion,
    FII buying, credit rating upgrade): add 10–20 points.
  • Sector headwind (policy tightening, input cost spike, demand slowdown news): subtract
    10–15 points.
  • Sector tailwind (govt capex announcement, PLI scheme, demand surge): add 10–15 points.
  • Macro bearishness despite current regime (RBI hawkishness, FII outflow surge,
    global risk-off): subtract 5–10 points from confidence_score directly.
  • If no news was fetched or headlines are irrelevant, keep fundamental_score at 60
    and note it in rationale — do NOT invent claims.
  • Headlines are unverified RSS items. Use them to flag risk, not to make positive
    claims. When in doubt, discount rather than inflate.

OUTPUT: Return ONLY valid JSON, no markdown, no explanation outside the JSON:
{
  "verdict": "TRADE" or "PASS",
  "technical_score": 0-100,
  "fundamental_score": 0-100,
  "confidence_score": 0-100,
  "rationale": "2–3 sentences: what specifically meets (or fails) the criteria",
  "disqualifying_factors": ["any red flags, even on a TRADE verdict"]
}"""


def _call_claude(
    regime: Regime,
    strategy: dict,
    indicators: dict,
    qual_context: str = "",
) -> Optional[dict]:
    """
    Sends pre-computed indicators + qualitative news context to Claude Sonnet 5
    for a trade verdict. Returns parsed JSON dict, or None on failure.
    """
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    ind = indicators
    above_50 = "ABOVE" if ind["above_sma50"] else "BELOW"
    above_200 = "ABOVE" if ind["above_sma200"] else "BELOW"
    flip_note = " ← JUST FLIPPED" if ind["supertrend_just_flipped"] else ""
    vol_note = " ← ELEVATED" if ind["volume_ratio_20d"] >= 1.5 else (" ← LOW" if ind["volume_ratio_20d"] < 0.8 else "")
    bb_note = " ← NEAR LOWER BAND" if ind["bollinger_position"] < 25 else (" ← NEAR UPPER BAND" if ind["bollinger_position"] > 75 else "")
    rsi_note = " ← OVERSOLD" if ind["rsi_14"] < 35 else (" ← OVERBOUGHT" if ind["rsi_14"] > 70 else "")
    wkh_note = " ← BREAKOUT ZONE" if ind["pct_from_52wk_high"] >= -1.5 else (" ← FAR FROM HIGH" if ind["pct_from_52wk_high"] < -20 else "")
    adx_note = " ← STRONG TREND" if ind.get("adx_14", 0) > 25 else (" ← NO TREND" if ind.get("adx_14", 0) < 20 else "")
    di_note = "+DI>-DI (bullish)" if ind.get("plus_di", 0) > ind.get("minus_di", 0) else "-DI>+DI (bearish)"
    cci_note = " ← OVERSOLD (<-100)" if ind.get("cci_20", 0) < -100 else (" ← OVERBOUGHT (>100)" if ind.get("cci_20", 0) > 100 else "")
    sq_note = "SQUEEZE (low-vol coil)" if ind.get("bb_squeeze") else "no squeeze"
    brk_note = " + BREAKING ABOVE UPPER BAND" if ind.get("bb_breaking_upper") else ""
    gc_note = ("EMA50>EMA200" if ind.get("ema50_above_ema200") else "EMA50<EMA200") + \
              (" ← GOLDEN CROSS just formed" if ind.get("golden_cross_recent") else "")

    qual_block = (
        f"\nQUALITATIVE CONTEXT (RSS headlines, unverified — use to flag risk, "
        f"not to make confident positive claims):\n{qual_context}\n"
        if qual_context
        else ""
    )

    prompt = f"""REGIME: {regime.value.upper()}
STRATEGY TO EVALUATE: {strategy["name"]}
Entry criteria: {strategy["description"]}

STOCK: {ind["ticker"]}
Pre-computed technical indicators (from Kite Connect daily OHLCV):
  LTP:                   ₹{ind["ltp"]:,.2f}
  RSI(14):               {ind["rsi_14"]}{rsi_note}
  Supertrend(10,3):      {ind["supertrend_10_3"]}{flip_note}
  Volume vs 20d avg:     {ind["volume_ratio_20d"]:.1f}×{vol_note}
  Distance from 52wk hi: {ind["pct_from_52wk_high"]:+.1f}%{wkh_note}
  Bollinger position:    {ind["bollinger_position"]:.0f}%{bb_note}
  vs 50-day SMA:         {above_50} (SMA ₹{ind["sma_50"]:,.2f})
  vs 200-day SMA:        {above_200} (SMA ₹{ind["sma_200"]:,.2f})
  ATR(14) as % of price: {ind["atr_pct"]:.2f}%
  ADX(14):               {ind.get("adx_14", 0)}{adx_note}  |  {di_note}
  CCI(20):               {ind.get("cci_20", 0)}{cci_note}
  EMA 50/200:            {gc_note}
  Bollinger bands:       {sq_note} (bandwidth {ind.get("bb_bandwidth_pct", 0)}%){brk_note}
{qual_block}
Does this stock meet the entry criteria for the "{strategy["name"]}" strategy?
Apply the qualitative weighing rules from your system prompt when scoring fundamental_score.
Output your verdict as JSON."""

    try:
        msg = client.messages.create(
            model=RESEARCHER_MODEL,
            max_tokens=2000,  # 500 was too low — claude-sonnet-5 ThinkingBlock consumes ~400 tokens before the JSON text
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        # claude-sonnet-5 may prepend a ThinkingBlock before the TextBlock.
        # Always locate the TextBlock explicitly instead of assuming content[0].
        text_block = next((b for b in msg.content if b.type == "text"), None)
        if not text_block:
            log.error(f"No text block in Claude response for {ind['ticker']}/{strategy['name']}")
            return None
        raw = text_block.text.strip()
        # Strip markdown fences if model adds them
        if "```" in raw:
            parts = raw.split("```")
            raw = parts[1] if len(parts) > 1 else parts[0]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw.strip())
    except json.JSONDecodeError as e:
        log.error(f"Claude returned invalid JSON for {ind['ticker']}/{strategy['name']}: {e}")
        return None
    except Exception as e:
        log.error(f"Anthropic API call failed for {ind['ticker']}: {e}")
        return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def generate_signal(regime_reading: RegimeReading) -> Optional[TradeSignal]:
    """
    Scans the universe, evaluates each ticker for the regime's strategy basket,
    returns the single highest-conviction signal above MIN_CONFIDENCE, or None.

    Returns None far more often than a TradeSignal. Frequency is the honest
    output of the threshold — not a target to optimise.
    """
    regime = regime_reading.regime
    baskets = STRATEGY_BASKETS.get(regime, [])

    if not baskets:
        log.info(f"No long strategies for {regime.value} — cycle skipped.")
        return None

    from trader.kite_client import get_kite_client

    kite = get_kite_client()

    # Load universe with sector + exchange info
    universe_entries = load_universe()
    sector_map = {e.ticker: e.sector for e in universe_entries}
    exchange_map = {e.ticker: e.exchange for e in universe_entries}
    tickers = [e.ticker for e in universe_entries]

    if not tickers:
        log.warning("Universe CSV is empty — nothing to scan.")
        return None

    # Build combined NSE+BSE instruments map.
    # BSE is fetched first so NSE takes precedence for dual-listed stocks
    # (most liquid venue is NSE for those names).
    try:
        bse_rows = kite.instruments("BSE")
        bse_map = {
            row["tradingsymbol"]: {"token": row["instrument_token"], "exchange": "BSE"}
            for row in bse_rows
            if row["instrument_type"] in ("EQ", "BE")
        }
        log.info(f"BSE instruments loaded: {len(bse_map)} EQ/BE symbols")
    except Exception as e:
        log.warning(f"Could not fetch BSE instruments (BSE stocks will be skipped): {e}")
        bse_map = {}

    try:
        nse_rows = kite.instruments("NSE")
        nse_map = {
            row["tradingsymbol"]: {"token": row["instrument_token"], "exchange": "NSE"}
            for row in nse_rows
            if row["instrument_type"] in ("EQ", "BE")  # BE = Trade-to-Trade/T2T segment
        }
        log.info(f"NSE instruments loaded: {len(nse_map)} EQ/BE symbols")
    except Exception as e:
        log.error(f"Could not fetch NSE instruments: {e}")
        nse_map = {}

    # Merge: NSE overrides BSE for dual-listed names
    instruments_map = {**bse_map, **nse_map}
    if not instruments_map:
        log.error("No instruments loaded from either exchange — aborting cycle.")
        return None

    from_date = (date.today() - timedelta(days=420)).strftime("%Y-%m-%d")
    to_date = date.today().strftime("%Y-%m-%d")

    from ledger.db import log_cycle_evaluation
    cycle_at = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")

    best_signal: Optional[TradeSignal] = None
    best_confidence = 0.0

    for ticker in tickers:
        info = instruments_map.get(ticker)
        if not info:
            log.warning(f"No instrument token for {ticker} on NSE or BSE — skipping")
            for strategy in baskets:
                log_cycle_evaluation(
                    cycle_at=cycle_at, regime=regime.value,
                    regime_confidence=regime_reading.confidence,
                    ticker=ticker, exchange=exchange_map.get(ticker, "NSE"),
                    strategy=strategy["name"], verdict="NO_TOKEN",
                )
            continue

        token = info["token"]
        resolved_exchange = info["exchange"]

        # Fetch OHLCV history
        try:
            hist = kite.historical_data(int(token), from_date, to_date, "day")
            if len(hist) < 30:
                log.debug(f"{ticker}: insufficient history ({len(hist)} bars)")
                for strategy in baskets:
                    log_cycle_evaluation(
                        cycle_at=cycle_at, regime=regime.value,
                        regime_confidence=regime_reading.confidence,
                        ticker=ticker, exchange=resolved_exchange,
                        strategy=strategy["name"], verdict=f"THIN_HISTORY ({len(hist)} bars)",
                    )
                continue
            indicators = _compute_indicators(hist, ticker)
        except Exception as e:
            log.error(f"History fetch failed for {ticker} ({resolved_exchange}): {e}")
            for strategy in baskets:
                log_cycle_evaluation(
                    cycle_at=cycle_at, regime=regime.value,
                    regime_confidence=regime_reading.confidence,
                    ticker=ticker, exchange=resolved_exchange,
                    strategy=strategy["name"], verdict="ERROR",
                    rationale=str(e),
                )
            continue

        # ---- Deterministic pre-filter: gate BEFORE the news fetch + Claude ----
        # Only strategies whose hard technical rules are met on this ticker are
        # sent onward. If none pass, skip the ticker entirely (no news, no LLM).
        passing = [s for s in baskets if _passes_prefilter(s["name"], indicators)]
        if not passing:
            for strategy in baskets:
                log_cycle_evaluation(
                    cycle_at=cycle_at, regime=regime.value,
                    regime_confidence=regime_reading.confidence,
                    ticker=ticker, exchange=resolved_exchange,
                    strategy=strategy["name"], verdict="PREFILTER_SKIP",
                    indicators=indicators,
                )
            continue

        # Fetch qualitative context once per ticker — only for pre-filtered
        # candidates (shared across the strategies that passed).
        qual_context = _fetch_qualitative_context(
            ticker=ticker,
            sector=sector_map.get(ticker, "Unknown"),
        )
        log.info(
            f"{ticker}: {len(passing)}/{len(baskets)} strateg"
            f"{'y' if len(passing) == 1 else 'ies'} passed pre-filter "
            f"({', '.join(s['name'] for s in passing)}) — news fetched, calling Claude"
        )

        # Evaluate each strategy that cleared the pre-filter
        for strategy in passing:
            log.debug(f"Evaluating {ticker} ({resolved_exchange}) / {strategy['name']}")
            verdict = _call_claude(regime, strategy, indicators, qual_context=qual_context)
            if not verdict:
                log_cycle_evaluation(
                    cycle_at=cycle_at, regime=regime.value,
                    regime_confidence=regime_reading.confidence,
                    ticker=ticker, exchange=resolved_exchange,
                    strategy=strategy["name"], verdict="ERROR",
                    indicators=indicators, rationale="Claude API call failed or returned invalid JSON",
                )
                continue

            try:
                confidence = float(verdict.get("confidence_score", 0))
                tech_score = float(verdict.get("technical_score", 0))
                fund_score = float(verdict.get("fundamental_score", 0))
            except (TypeError, ValueError) as e:
                log.error(f"Malformed scores from Claude for {ticker}/{strategy['name']}: {e}")
                log_cycle_evaluation(
                    cycle_at=cycle_at, regime=regime.value,
                    regime_confidence=regime_reading.confidence,
                    ticker=ticker, exchange=resolved_exchange,
                    strategy=strategy["name"], verdict="ERROR",
                    indicators=indicators, rationale=f"Malformed scores in Claude verdict: {e}",
                )
                continue
            rationale  = verdict.get("rationale", "")

            if verdict.get("verdict") != "TRADE":
                log.debug(f"  PASS — {rationale[:80]}")
                log_cycle_evaluation(
                    cycle_at=cycle_at, regime=regime.value,
                    regime_confidence=regime_reading.confidence,
                    ticker=ticker, exchange=resolved_exchange,
                    strategy=strategy["name"], verdict="PASS",
                    indicators=indicators,
                    technical_score=tech_score, fundamental_score=fund_score,
                    confidence_score=confidence, rationale=rationale,
                )
                continue

            if confidence < MIN_CONFIDENCE:
                log.debug(f"  Below threshold: {confidence:.0f}% < {MIN_CONFIDENCE:.0f}%")
                log_cycle_evaluation(
                    cycle_at=cycle_at, regime=regime.value,
                    regime_confidence=regime_reading.confidence,
                    ticker=ticker, exchange=resolved_exchange,
                    strategy=strategy["name"], verdict=f"BELOW_THRESHOLD ({confidence:.0f}%)",
                    indicators=indicators,
                    technical_score=tech_score, fundamental_score=fund_score,
                    confidence_score=confidence, rationale=rationale,
                )
                continue

            log.info(
                f"Candidate: {ticker} ({resolved_exchange}) / {strategy['name']} "
                f"— confidence {confidence:.0f}% | "
                f"tech {tech_score:.0f} / fund {fund_score:.0f}"
            )
            log_cycle_evaluation(
                cycle_at=cycle_at, regime=regime.value,
                regime_confidence=regime_reading.confidence,
                ticker=ticker, exchange=resolved_exchange,
                strategy=strategy["name"], verdict="TRADE",
                indicators=indicators,
                technical_score=tech_score, fundamental_score=fund_score,
                confidence_score=confidence, rationale=rationale,
            )

            if confidence > best_confidence:
                # Build defensively. A missing/None indicator here used to raise
                # (e.g. formatting None with :.1f / :+.0f), which crashed the
                # WHOLE cycle's generate_signal after this TRADE was already
                # logged to cycle_log — so the candidate showed as TRADE in the
                # summary but never became a signal or an alert. Never again:
                # a single ticker's data quirk must not lose a found candidate.
                _vr = indicators.get("volume_ratio_20d")
                _pc = indicators.get("pct_from_52wk_high")
                _vr_s = ("%.1f" % _vr) if isinstance(_vr, (int, float)) else "n/a"
                _pc_s = ("%+.0f" % _pc) if isinstance(_pc, (int, float)) else "n/a"
                try:
                    _sig = TradeSignal(
                        ticker=ticker,
                        sector=sector_map.get(ticker, "Unknown"),
                        exchange=resolved_exchange,
                        regime=regime,
                        strategy_bucket=strategy["name"],
                        direction="BUY",
                        technical_score=tech_score,
                        fundamental_score=fund_score,
                        confidence_score=confidence,
                        rationale=(
                            f"{rationale} "
                            f"[tech {indicators.get('rsi_14')} RSI | "
                            f"{indicators.get('supertrend_10_3')} ST | "
                            f"{_vr_s}x vol | {_pc_s}% 52wkH]"
                        ),
                    )
                    best_signal = _sig
                    best_confidence = confidence
                except Exception as e:
                    log.error(
                        f"Failed to build TradeSignal for {ticker}/{strategy['name']} "
                        f"(scan continues, prior best_signal preserved): {e}",
                        exc_info=True,
                    )

    if best_signal:
        log.info(
            f"Best signal this cycle: {best_signal.ticker} @ "
            f"{best_signal.confidence_score:.0f}% confidence"
        )
    else:
        log.info("No signals cleared the confidence threshold this cycle — nothing to trade.")

    return best_signal
