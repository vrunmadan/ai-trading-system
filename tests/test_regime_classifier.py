"""
Tests for researcher/regime_classifier.py.

Two layers:
  1. Pure math (_score_components, _map_score_to_regime, _ema, _sma) — no
     mocking needed, these take plain floats/lists.
  2. classify_regime()'s apply_inertia wiring — needs a mocked Kite client
     since the function fetches live market data. Focused specifically on
     the apply_inertia=False fix (position monitor / /diagnose_cycle should
     not consume a slot in the hourly cycle's regime-inertia smoothing).
"""

import pytest

import researcher.regime_classifier as rc
from researcher.regime_classifier import (
    Regime,
    _score_components,
    _map_score_to_regime,
    _ema,
    _sma,
    classify_regime,
)


# ---------------------------------------------------------------------------
# Pure math
# ---------------------------------------------------------------------------

class TestEmaSma:
    def test_ema_falls_back_to_simple_average_with_short_history(self):
        assert _ema([10.0, 20.0], period=5) == pytest.approx(15.0)

    def test_sma_falls_back_to_simple_average_with_short_history(self):
        assert _sma([10.0, 20.0], period=5) == pytest.approx(15.0)

    def test_sma_uses_only_the_last_n_prices(self):
        prices = [100.0] * 10 + [200.0] * 5
        assert _sma(prices, period=5) == pytest.approx(200.0)


class TestScoreComponents:
    def test_calm_bullish_conditions_score_positive(self):
        score, parts = _score_components(
            nifty_pct=5.0, vix=12.0, vix_5d_change=0.0, breadth_pct=70.0
        )
        assert score > 0
        # 3 parts: nifty, vix level, breadth. VIX-momentum only adds a 4th
        # part when vix_5d_change is past +/-10/+20 — 0.0 doesn't trigger it.
        assert len(parts) == 3

    def test_extreme_fear_scores_very_negative(self):
        score, _ = _score_components(
            nifty_pct=-15.0, vix=32.0, vix_5d_change=25.0, breadth_pct=20.0
        )
        assert score < -6.0

    def test_vix_momentum_only_scores_at_the_extremes(self):
        # A middling VIX change (between -10 and +20) shouldn't add/subtract
        base_score, _ = _score_components(0.0, 18.0, vix_5d_change=5.0, breadth_pct=50.0)
        falling_score, _ = _score_components(0.0, 18.0, vix_5d_change=-15.0, breadth_pct=50.0)
        rising_score, _ = _score_components(0.0, 18.0, vix_5d_change=25.0, breadth_pct=50.0)
        assert falling_score > base_score
        assert rising_score < base_score


class TestMapScoreToRegime:
    def test_extreme_vix_hard_overrides_to_crash_regardless_of_score(self):
        regime, confidence = _map_score_to_regime(score=5.0, nifty_pct=3.0, vix=31.0)
        assert regime == Regime.CRASH

    def test_deep_nifty_selloff_hard_overrides_to_crash(self):
        regime, confidence = _map_score_to_regime(score=5.0, nifty_pct=-13.0, vix=15.0)
        assert regime == Regime.CRASH

    def test_euphoria_requires_both_high_score_and_extended_nifty(self):
        # High score but Nifty not extended enough → BULL, not EUPHORIA
        regime, _ = _map_score_to_regime(score=6.0, nifty_pct=3.0, vix=12.0)
        assert regime == Regime.BULL

        regime, _ = _map_score_to_regime(score=6.0, nifty_pct=9.0, vix=12.0)
        assert regime == Regime.EUPHORIA

    def test_sideways_boundary(self):
        regime, _ = _map_score_to_regime(score=0.0, nifty_pct=0.0, vix=17.0)
        assert regime == Regime.SIDEWAYS

    def test_weak_score_stays_bear_not_crash_if_vix_and_nifty_are_manageable(self):
        # score < -6 normally → CRASH, but not if VIX is still under
        # VIX_HIGH_FEAR (25) and Nifty hasn't collapsed past the crash band.
        regime, _ = _map_score_to_regime(score=-7.0, nifty_pct=-5.0, vix=22.0)
        assert regime == Regime.BEAR

    def test_confidence_is_always_at_least_40(self):
        for score in (-10.0, -2.0, 0.0, 2.0, 10.0):
            _, confidence = _map_score_to_regime(score, nifty_pct=0.0, vix=18.0)
            assert confidence >= 40.0


# ---------------------------------------------------------------------------
# apply_inertia wiring (mocked Kite client)
# ---------------------------------------------------------------------------

class _FakeKite:
    """Minimal Kite client stub — enough surface for classify_regime()."""

    def instruments(self, exchange):
        return [{"tradingsymbol": "DUMMY", "instrument_token": 1, "instrument_type": "EQ"}]

    def historical_data(self, token, from_date, to_date, interval, **kwargs):
        # 60 flat daily closes — deterministic score of ~0 (sideways-ish)
        return [{"close": 22000.0}] * 60

    def ltp(self, keys):
        return {k: {"last_price": 22000.0} for k in ([keys] if isinstance(keys, str) else keys)}


@pytest.fixture(autouse=True)
def reset_regime_history():
    """_regime_history is module-level state — reset it around every test."""
    rc._regime_history = []
    yield
    rc._regime_history = []


@pytest.fixture
def mocked_kite(monkeypatch):
    fake = _FakeKite()
    monkeypatch.setattr("trader.kite_client.get_kite_client", lambda: fake)
    monkeypatch.setattr("universe.loader.get_tickers", lambda: ["DUMMY"])
    return fake


class TestApplyInertiaWiring:
    def test_default_call_pushes_into_regime_history(self, mocked_kite):
        assert rc._regime_history == []
        classify_regime()
        assert len(rc._regime_history) == 1

    def test_apply_inertia_false_does_not_mutate_regime_history(self, mocked_kite):
        assert rc._regime_history == []
        classify_regime(apply_inertia=False)
        assert rc._regime_history == []

    def test_apply_inertia_false_does_not_disturb_an_in_progress_smoothing_window(self, mocked_kite):
        # Two real hourly-cycle calls build up history...
        classify_regime()
        classify_regime()
        history_after_two_cycles = list(rc._regime_history)

        # ...an out-of-band monitor/debug call must not add a third entry.
        classify_regime(apply_inertia=False)
        assert rc._regime_history == history_after_two_cycles


# ---------------------------------------------------------------------------
# Fixed 2026-09-22: _fetch_nifty_vs_200ema/_fetch_vix/_fetch_breadth used to
# fall back to hardcoded "neutral" numbers (Nifty 22000.0, VIX 18.0, breadth
# 50.0) when ALL real data sources failed -- indistinguishable from a real
# reading, and able to combine into a confident (including BULL) regime
# built entirely on invented data. They now return None on total failure,
# and classify_regime() refuses to classify on that, returning a flagged,
# zero-confidence SIDEWAYS read instead.
# ---------------------------------------------------------------------------

class _RaisingKite:
    """Every call fails -- simulates a total data outage."""

    def instruments(self, exchange):
        return []

    def historical_data(self, *a, **k):
        raise ConnectionError("simulated outage")

    def ltp(self, *a, **k):
        raise ConnectionError("simulated outage")


class TestFetchersReturnNoneOnTotalFailure:
    def test_nifty_returns_none_when_history_and_ltp_both_fail(self):
        assert rc._fetch_nifty_vs_200ema(_RaisingKite()) is None

    def test_vix_returns_none_when_history_and_ltp_both_fail(self):
        assert rc._fetch_vix(_RaisingKite()) is None

    def test_breadth_returns_none_when_no_sampled_ticker_resolves(self):
        instruments_map = {"DUMMY": 1}
        assert rc._fetch_breadth(_RaisingKite(), ["DUMMY"], instruments_map) is None

    def test_nifty_still_returns_real_ltp_when_only_history_fails(self):
        # LTP-only is real data (just no 200-EMA trend) -- must NOT be
        # treated the same as a total failure.
        class _HistoryFailsLtpWorks:
            def historical_data(self, *a, **k):
                raise ConnectionError("simulated")

            def ltp(self, keys):
                return {"NSE:NIFTY 50": {"last_price": 25000.0}}

        result = rc._fetch_nifty_vs_200ema(_HistoryFailsLtpWorks())
        assert result == (25000.0, 0.0)

    def test_vix_still_returns_real_ltp_when_only_history_fails(self):
        class _HistoryFailsLtpWorks:
            def historical_data(self, *a, **k):
                raise ConnectionError("simulated")

            def ltp(self, keys):
                return {"NSE:INDIA VIX": {"last_price": 14.0}}

        result = rc._fetch_vix(_HistoryFailsLtpWorks())
        assert result == (14.0, 0.0)


class TestClassifyRegimeDegradedRead:
    def test_total_data_outage_returns_flagged_zero_confidence_sideways(self, monkeypatch):
        monkeypatch.setattr("trader.kite_client.get_kite_client", lambda: _RaisingKite())
        monkeypatch.setattr("universe.loader.get_tickers", lambda: ["DUMMY"])

        reading = classify_regime()

        assert reading.regime == Regime.SIDEWAYS
        assert reading.confidence == 0.0
        assert "DATA UNAVAILABLE" in reading.rationale
        assert "Nifty 50" in reading.rationale
        assert "India VIX" in reading.rationale
        assert "market breadth" in reading.rationale

    def test_degraded_read_does_not_fabricate_market_levels(self, monkeypatch):
        monkeypatch.setattr("trader.kite_client.get_kite_client", lambda: _RaisingKite())
        monkeypatch.setattr("universe.loader.get_tickers", lambda: ["DUMMY"])

        reading = classify_regime()

        # Explicitly NOT the old hardcoded 22000.0 / 18.0 fabricated levels.
        assert reading.nifty_vs_ema_pct == 0.0
        assert reading.vix == 0.0
        assert reading.breadth_pct == 0.0

    def test_degraded_read_does_not_pollute_inertia_history(self, monkeypatch):
        monkeypatch.setattr("trader.kite_client.get_kite_client", lambda: _RaisingKite())
        monkeypatch.setattr("universe.loader.get_tickers", lambda: ["DUMMY"])

        assert rc._regime_history == []
        classify_regime()
        assert rc._regime_history == []  # never entered inertia smoothing

    def test_partial_failure_is_not_treated_as_a_total_outage(self, monkeypatch):
        """
        Nifty/VIX history fail but their LTP fallbacks succeed, and breadth
        resolves normally -- this is real (if thinner) data, not fabricated,
        so classify_regime must NOT take the degraded path.
        """

        class _PartialFailKite:
            def instruments(self, exchange):
                return [{"tradingsymbol": "DUMMY", "instrument_token": 1, "instrument_type": "EQ"}]

            def historical_data(self, token, from_date, to_date, interval, **kwargs):
                if token in (rc.INDIA_VIX_TOKEN, rc.NIFTY50_TOKEN):
                    raise ConnectionError("simulated index-history outage")
                return [{"close": 100.0 + i} for i in range(60)]  # breadth ticker

            def ltp(self, keys):
                ks = [keys] if isinstance(keys, str) else keys
                return {k: {"last_price": 14.0 if "VIX" in k else 25000.0} for k in ks}

        monkeypatch.setattr("trader.kite_client.get_kite_client", lambda: _PartialFailKite())
        monkeypatch.setattr("universe.loader.get_tickers", lambda: ["DUMMY"])

        reading = classify_regime()

        assert "DATA UNAVAILABLE" not in reading.rationale
        assert reading.vix == 14.0
        assert reading.nifty_vs_ema_pct == 0.0  # LTP-only fallback assumes neutral vs EMA


class TestFetchersCallDropIncompleteTodayBar:
    """
    Wiring check: _fetch_nifty_vs_200ema/_fetch_vix/_fetch_breadth must each
    call trader.kite_client.drop_incomplete_today_bar() on whatever Kite
    returns, before computing anything from it. Spies on the (module-level)
    function rather than faking wall-clock time, since
    drop_incomplete_today_bar()'s own before/after-close behaviour is
    already exhaustively covered in test_market_session.py -- this only
    needs to prove each call site actually invokes it.
    """

    def _install_spy(self, monkeypatch):
        calls = []

        def _spy(hist, now=None):
            calls.append(list(hist))
            return hist[:-1] if hist else hist  # simulate "today's bar dropped"

        monkeypatch.setattr("trader.kite_client.drop_incomplete_today_bar", _spy)
        return calls

    def test_nifty_fetch_calls_it_and_uses_the_trimmed_result(self, monkeypatch):
        calls = self._install_spy(monkeypatch)

        class _Kite:
            def historical_data(self, token, from_date, to_date, interval, **kwargs):
                return [{"close": 100.0}] * 60  # last one would be "today"

        result = rc._fetch_nifty_vs_200ema(_Kite())

        assert len(calls) == 1
        assert len(calls[0]) == 60  # spy was handed the untrimmed 60 bars
        assert result is not None  # fetcher still works off the trimmed 59

    def test_vix_fetch_calls_it_and_uses_the_trimmed_result(self, monkeypatch):
        calls = self._install_spy(monkeypatch)

        class _Kite:
            def historical_data(self, token, from_date, to_date, interval, **kwargs):
                return [{"close": 15.0}] * 10

        result = rc._fetch_vix(_Kite())

        assert len(calls) == 1
        assert len(calls[0]) == 10
        assert result is not None

    def test_breadth_fetch_calls_it_per_ticker(self, monkeypatch):
        calls = self._install_spy(monkeypatch)

        class _Kite:
            def historical_data(self, token, from_date, to_date, interval, **kwargs):
                return [{"close": 100.0 + i} for i in range(60)]

        instruments_map = {"DUMMY": 1}
        result = rc._fetch_breadth(_Kite(), ["DUMMY"], instruments_map)

        assert len(calls) == 1  # one sampled ticker -> one historical_data call
        assert result is not None
