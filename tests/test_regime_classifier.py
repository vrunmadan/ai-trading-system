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
