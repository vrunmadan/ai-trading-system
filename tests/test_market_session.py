"""
Tests for trader/kite_client.py's drop_incomplete_today_bar()/_bar_date().

Bug fixed 2026-09-22: every daily indicator in this system (RSI, ATR,
Supertrend, volume ratio, 52wk proximity, Bollinger, ADX, CCI, EMAs, plus
the regime classifier's Nifty/VIX/breadth inputs) used to be computed
directly on whatever Kite's "day"-interval historical_data() returned,
including an in-progress bar for the current session while the market is
still open. That created a mechanical time-of-day bias in signal
generation unrelated to any stock's actual setup, and diverged from
streak_backtests/backtest.py, which only ever evaluates fully-settled
historical bars. drop_incomplete_today_bar() drops that live bar so live
indicators are computed on the same kind of data the backtest was
validated against.
"""

from datetime import date, datetime

import pytz

from trader.kite_client import _bar_date, drop_incomplete_today_bar

IST = pytz.timezone("Asia/Kolkata")


def _ist(y, m, d, hh, mm):
    return IST.localize(datetime(y, m, d, hh, mm))


class TestBarDate:
    def test_datetime_field_extracts_date(self):
        bar = {"date": _ist(2026, 9, 22, 9, 15)}
        assert _bar_date(bar) == date(2026, 9, 22)

    def test_plain_date_field_passes_through(self):
        bar = {"date": date(2026, 9, 22)}
        assert _bar_date(bar) == date(2026, 9, 22)

    def test_iso_string_field_parses(self):
        bar = {"date": "2026-09-22T09:15:00+0530"}
        assert _bar_date(bar) == date(2026, 9, 22)

    def test_missing_date_key_returns_none(self):
        assert _bar_date({"close": 100.0}) is None

    def test_unparseable_string_returns_none(self):
        assert _bar_date({"date": "not-a-date"}) is None

    def test_unexpected_type_returns_none(self):
        assert _bar_date({"date": 12345}) is None


class TestDropIncompleteTodayBar:
    def test_empty_hist_is_unchanged(self):
        assert drop_incomplete_today_bar([]) == []

    def test_todays_bar_dropped_before_market_close(self):
        now = _ist(2026, 9, 22, 11, 0)  # 11:00 AM — market open, not closed
        hist = [
            {"date": _ist(2026, 9, 21, 0, 0), "close": 100.0},
            {"date": _ist(2026, 9, 22, 0, 0), "close": 101.0},  # today, live
        ]
        result = drop_incomplete_today_bar(hist, now=now)
        assert len(result) == 1
        assert result[0]["close"] == 100.0

    def test_todays_bar_kept_after_market_close(self):
        now = _ist(2026, 9, 22, 16, 0)  # 4:00 PM — well after 15:30 close
        hist = [
            {"date": _ist(2026, 9, 21, 0, 0), "close": 100.0},
            {"date": _ist(2026, 9, 22, 0, 0), "close": 101.0},  # today, settled
        ]
        result = drop_incomplete_today_bar(hist, now=now)
        assert len(result) == 2

    def test_boundary_exactly_at_market_close_is_kept(self):
        now = _ist(2026, 9, 22, 15, 30)  # exactly 15:30 — session just ended
        hist = [{"date": _ist(2026, 9, 22, 0, 0), "close": 101.0}]
        result = drop_incomplete_today_bar(hist, now=now)
        assert len(result) == 1  # >= close counts as settled, not dropped

    def test_boundary_one_minute_before_close_is_dropped(self):
        now = _ist(2026, 9, 22, 15, 29)
        hist = [
            {"date": _ist(2026, 9, 21, 0, 0), "close": 100.0},
            {"date": _ist(2026, 9, 22, 0, 0), "close": 101.0},
        ]
        result = drop_incomplete_today_bar(hist, now=now)
        assert len(result) == 1

    def test_last_bar_not_todays_is_never_dropped(self):
        # Fetched before market open: Kite hasn't produced a bar for today
        # yet, so the last bar is yesterday's already-settled close.
        now = _ist(2026, 9, 22, 8, 0)
        hist = [
            {"date": _ist(2026, 9, 20, 0, 0), "close": 99.0},
            {"date": _ist(2026, 9, 21, 0, 0), "close": 100.0},
        ]
        result = drop_incomplete_today_bar(hist, now=now)
        assert len(result) == 2

    def test_missing_date_field_fails_safe_to_unchanged(self):
        now = _ist(2026, 9, 22, 11, 0)
        hist = [{"close": 100.0}, {"close": 101.0}]  # no 'date' key at all
        result = drop_incomplete_today_bar(hist, now=now)
        assert result == hist

    def test_defaults_to_real_now_when_not_provided(self):
        # Smoke test only: must not raise when `now` is omitted.
        hist = [{"date": date.today(), "close": 100.0}]
        result = drop_incomplete_today_bar(hist)
        assert isinstance(result, list)
