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
from datetime import date, timedelta
from typing import Optional

import anthropic

from researcher.regime_classifier import Regime, RegimeReading
from universe.loader import get_tickers, load_universe

log = logging.getLogger(__name__)

RESEARCHER_MODEL = os.getenv("RESEARCHER_MODEL", "claude-sonnet-5")
MIN_CONFIDENCE = float(os.getenv("MIN_SIGNAL_CONFIDENCE", "75"))


@dataclass
class TradeSignal:
    ticker: str
    sector: str            # from universe CSV (for Risk Sizer sector check)
    regime: Regime
    strategy_bucket: str
    direction: str         # "BUY" (system only does longs currently)
    technical_score: float  # 0-100
    fundamental_score: float  # 0-100
    confidence_score: float   # 0-100 weighted composite
    rationale: str


# ---------------------------------------------------------------------------
# Strategy baskets — one per regime bucket.
# Empty list = no longs (Bear / Crash / Euphoria).
# Each strategy has a name + plain-English entry criteria passed to Claude.
# These are the strategies that passed Streak backtesting. Add new ones only
# after a Streak backtest in streak_backtests/ shows positive expectancy.
# ---------------------------------------------------------------------------
STRATEGY_BASKETS: dict[Regime, list[dict]] = {
    Regime.BULL: [
        {
            "name": "52wk_breakout",
            "description": (
                "Entry: LTP has just broken above OR is within 1% of the 52-week high. "
                "Volume today is ≥ 1.5x the 20-day average (breakout confirmation). "
                "RSI(14) is 50–70 (momentum but not overbought). "
                "Supertrend(10,3) is GREEN. "
                "Thesis: momentum continuation after a proven resistance level is cleared "
                "with volume. Do NOT fire on a high-RSI stock near its 52-week high if "
                "volume is below average — that's a grind, not a breakout."
            ),
        },
        {
            "name": "supertrend_buy",
            "description": (
                "Entry: Supertrend(10,3) has JUST flipped from RED to GREEN on the daily chart "
                "(flag if the flip happened within the last 2 bars). "
                "LTP is above the 50-day SMA. RSI(14) is 40–65. "
                "Thesis: trend-following entry on a confirmed momentum shift. "
                "Do NOT fire if the flip happened > 5 bars ago — you'd be chasing."
            ),
        },
    ],
    Regime.SIDEWAYS: [
        {
            "name": "rsi_mean_reversion",
            "description": (
                "Entry: daily RSI(14) < 35 AND Bollinger position < 25% (near lower band). "
                "Volume declining over the past 3 days (selling exhaustion). "
                "Stock is NOT making new 52-week lows (avoid falling knives). "
                "Thesis: oversold bounce in a range-bound market. "
                "Do NOT fire if RSI is declining fast (still in freefall) — wait for "
                "RSI to turn up before entry."
            ),
        },
    ],
    Regime.BEAR: [],      # No new longs in a bear market
    Regime.CRASH: [],     # Capital preservation — no longs
    Regime.EUPHORIA: [],  # No new entries — tighten stops on existing only
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
    }


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

    # Load universe with sector info (needed for Risk Sizer sector check)
    universe_entries = load_universe()
    sector_map = {e.ticker: e.sector for e in universe_entries}
    tickers = [e.ticker for e in universe_entries]

    if not tickers:
        log.warning("Universe CSV is empty — nothing to scan.")
        return None

    # Fetch instruments list once; reuse for all tickers
    try:
        instruments_map = {
            row["tradingsymbol"]: row["instrument_token"]
            for row in kite.instruments("NSE")
            if row["instrument_type"] in ("EQ", "BE")  # BE = Trade-to-Trade/T2T segment
        }
    except Exception as e:
        log.error(f"Could not fetch instruments list: {e}")
        instruments_map = {}

    from_date = (date.today() - timedelta(days=420)).strftime("%Y-%m-%d")
    to_date = date.today().strftime("%Y-%m-%d")

    best_signal: Optional[TradeSignal] = None
    best_confidence = 0.0

    for ticker in tickers:
        token = instruments_map.get(ticker)
        if not token:
            log.warning(f"No instrument token for {ticker} — skipping")
            continue

        # Fetch OHLCV history
        try:
            hist = kite.historical_data(int(token), from_date, to_date, "day")
            if len(hist) < 30:
                log.debug(f"{ticker}: insufficient history ({len(hist)} bars)")
                continue
            indicators = _compute_indicators(hist, ticker)
        except Exception as e:
            log.error(f"History fetch failed for {ticker}: {e}")
            continue

        # Fetch qualitative context once per ticker (shared across strategies)
        qual_context = _fetch_qualitative_context(
            ticker=ticker,
            sector=sector_map.get(ticker, "Unknown"),
        )
        log.debug(f"{ticker}: qualitative context fetched ({len(qual_context)} chars)")

        # Evaluate each strategy in the basket
        for strategy in baskets:
            log.debug(f"Evaluating {ticker} / {strategy['name']}")
            verdict = _call_claude(regime, strategy, indicators, qual_context=qual_context)
            if not verdict:
                continue

            if verdict.get("verdict") != "TRADE":
                log.debug(f"  PASS — {verdict.get('rationale', '')[:80]}")
                continue

            confidence = float(verdict.get("confidence_score", 0))
            if confidence < MIN_CONFIDENCE:
                log.debug(f"  Confidence {confidence:.0f}% < threshold {MIN_CONFIDENCE:.0f}%")
                continue

            log.info(
                f"Candidate: {ticker} / {strategy['name']} "
                f"— confidence {confidence:.0f}% | "
                f"tech {verdict.get('technical_score', 0):.0f} / "
                f"fund {verdict.get('fundamental_score', 0):.0f}"
            )

            if confidence > best_confidence:
                best_confidence = confidence
                best_signal = TradeSignal(
                    ticker=ticker,
                    sector=sector_map.get(ticker, "Unknown"),
                    regime=regime,
                    strategy_bucket=strategy["name"],
                    direction="BUY",
                    technical_score=float(verdict.get("technical_score", 0)),
                    fundamental_score=float(verdict.get("fundamental_score", 0)),
                    confidence_score=confidence,
                    rationale=(
                        f"{verdict.get('rationale', '')} "
                        f"[tech {indicators['rsi_14']} RSI | "
                        f"{indicators['supertrend_10_3']} ST | "
                        f"{indicators['volume_ratio_20d']:.1f}x vol | "
                        f"{indicators['pct_from_52wk_high']:+.0f}% 52wkH]"
                    ),
                )

    if best_signal:
        log.info(
            f"Best signal this cycle: {best_signal.ticker} @ "
            f"{best_signal.confidence_score:.0f}% confidence"
        )
    else:
        log.info("No signals cleared the confidence threshold this cycle — nothing to trade.")

    return best_signal
