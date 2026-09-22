"""
Tests for _atr_series/_atr_pct and the _supertrend refactor that now shares
them (researcher/signal_generator.py).

Bug fixed 2026-09-22: _compute_indicators()'s "atr_pct" used
(14-day-high - 14-day-low) / ltp / 14 * 100 -- the TOTAL high-low span of
the 14-day window divided by 14 -- while every strategy description and
Claude's own prompt call it "ATR(14)", i.e. Wilder's average of 14 DAILY
true-range values. On real data the two formulas diverge badly (the old
one reported roughly 0.2-0.3x the real ATR): the window-span formula
collapses two weeks of back-and-forth price action into a single number,
throwing away exactly the day-to-day noise ATR exists to measure.
_supertrend() already computed real Wilder's ATR correctly, just inline
and un-exported -- _atr_series() factors that logic out so both indicators
now share one implementation instead of silently drifting apart again.
"""

from researcher.signal_generator import _atr_series, _atr_pct, _supertrend


def _flat_ohlc(n: int, close: float = 100.0, half_range: float = 1.0):
    """n bars of perfectly flat OHLC: every day's true range is identical
    (2 * half_range), so Wilder's ATR is exactly 2 * half_range at every
    point -- a clean, hand-verifiable fixture."""
    closes = [close] * n
    highs = [close + half_range] * n
    lows = [close - half_range] * n
    return highs, lows, closes


class TestAtrSeries:
    def test_constant_true_range_gives_constant_atr(self):
        highs, lows, closes = _flat_ohlc(20)  # TR = 2.0 every day
        series = _atr_series(highs, lows, closes, period=14)
        assert series == [2.0] * 6  # 19 TR values, 14 seed + 5 smoothed

    def test_exact_boundary_length_gives_single_value(self):
        # len(closes) == period + 1 -> exactly 14 TR values -> one ATR value
        highs, lows, closes = _flat_ohlc(15)
        series = _atr_series(highs, lows, closes, period=14)
        assert series == [2.0]

    def test_insufficient_history_returns_empty(self):
        highs, lows, closes = _flat_ohlc(10)  # < period + 1
        assert _atr_series(highs, lows, closes, period=14) == []

    def test_gap_days_count_toward_true_range(self):
        # A close-to-close gap larger than any single day's high-low range
        # must still show up in ATR (this is what separates true range from
        # a plain high-low range, and from the old buggy window-span formula).
        highs = [100.0] * 16
        lows = [99.0] * 16
        closes = [99.5] * 15 + [110.0]  # day 16 gaps way up from prior close
        series = _atr_series(highs, lows, closes, period=14)
        # TR on the gap day = |high - prev_close| = |100 - 99.5|... prev close
        # for day 16 is closes[14] = 99.5, but day 16's own high/low are still
        # 100/99 -- so TR = max(1, |100-99.5|, |99-99.5|) = 1. Use a case where
        # the gap is in the *next* day's high/low instead, so it's unambiguous:
        highs = [100.0] * 15 + [112.0]
        lows = [99.0] * 15 + [111.0]
        closes = [99.5] * 15 + [99.5]
        series = _atr_series(highs, lows, closes, period=14)
        last_tr = max(112.0 - 111.0, abs(112.0 - 99.5), abs(111.0 - 99.5))
        assert last_tr == 12.5  # the gap dominates the plain 1.0 high-low range
        assert series[-1] > 1.0  # smoothed ATR picked up the gap, not just 1.0


class TestAtrPct:
    def test_matches_series_as_percent_of_price(self):
        highs, lows, closes = _flat_ohlc(20, close=100.0, half_range=1.0)
        assert _atr_pct(highs, lows, closes, ltp=100.0) == 2.0

    def test_scales_with_price(self):
        # Same absolute ATR, higher price -> smaller %.
        highs, lows, closes = _flat_ohlc(20, close=1000.0, half_range=1.0)
        assert _atr_pct(highs, lows, closes, ltp=1000.0) == 0.2

    def test_insufficient_history_returns_zero_not_error(self):
        highs, lows, closes = _flat_ohlc(5)
        assert _atr_pct(highs, lows, closes, ltp=100.0) == 0.0

    def test_not_the_old_buggy_window_span_formula(self):
        # Regression guard: the old formula was
        # (max(highs[-14:]) - min(lows[-14:])) / ltp / 14 * 100. On this
        # flat fixture that gives (101-99)/100/14*100 = 0.14, vs. the
        # correct 2.0 -- roughly 14x too small, well past the "0.2-0.3x"
        # divergence the 2026-09-22 audit flagged on real (less extreme)
        # data. Assert we're nowhere near that old, wrong value.
        highs, lows, closes = _flat_ohlc(20, close=100.0, half_range=1.0)
        old_buggy_value = round((max(highs[-14:]) - min(lows[-14:])) / 100.0 / 14 * 100, 2)
        assert old_buggy_value == 0.14
        assert _atr_pct(highs, lows, closes, ltp=100.0) == 2.0
        assert _atr_pct(highs, lows, closes, ltp=100.0) > old_buggy_value * 5


class TestSupertrendStillWorksAfterRefactor:
    """
    _supertrend() used to compute its own ATR inline; it now calls the
    shared _atr_series(). These are basic direction sanity checks (not a
    hand-verified golden value) to confirm the refactor didn't change its
    externally-visible behaviour, since the loop body moved but wasn't
    otherwise rewritten.
    """

    def test_thin_history_returns_unknown(self):
        highs, lows, closes = _flat_ohlc(5)  # < period(10) + 3
        direction, flipped = _supertrend(highs, lows, closes)
        assert direction == "UNKNOWN"
        assert flipped is False

    def test_strong_uptrend_is_green(self):
        n = 40
        closes = [100.0 + 2 * i for i in range(n)]
        highs = [c + 1 for c in closes]
        lows = [c - 1 for c in closes]
        direction, _ = _supertrend(highs, lows, closes)
        assert direction == "GREEN"

    def test_strong_downtrend_is_red(self):
        n = 40
        closes = [200.0 - 2 * i for i in range(n)]
        highs = [c + 1 for c in closes]
        lows = [c - 1 for c in closes]
        direction, _ = _supertrend(highs, lows, closes)
        assert direction == "RED"

    def test_uptrend_then_sharp_reversal_flips_to_red(self):
        # A hard multi-day crash after a clean uptrend must eventually turn
        # the direction RED. just_flipped only covers the last 2 bars, and
        # a 5-bar crash can plausibly flip a bar or two before the very
        # last one -- so this checks the settled direction, not the exact
        # bar the flip lands on.
        n = 30
        up = [100.0 + 2 * i for i in range(n)]
        crash = [up[-1] - 5 * i for i in range(1, 6)]
        closes = up + crash
        highs = [c + 1 for c in closes]
        lows = [c - 1 for c in closes]
        direction, flipped = _supertrend(highs, lows, closes)
        assert direction == "RED"
        assert isinstance(flipped, bool)

    def test_flip_on_final_bar_is_reported(self):
        # A single sharp drop landing exactly on the last bar must be
        # reported as just_flipped=True.
        n = 25
        up = [100.0 + 2 * i for i in range(n)]
        closes = up + [up[-1] - 30.0]  # one hard drop on the final bar
        highs = [c + 1 for c in closes]
        lows = [c - 1 for c in closes]
        direction, flipped = _supertrend(highs, lows, closes)
        assert direction == "RED"
        assert flipped is True
