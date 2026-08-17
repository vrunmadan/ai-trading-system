"""
Tests for trader/kite_client.py's microstructure_checks() — the deterministic
pre-trade gates (circuit buffer, liquidity, ADV) that run before every order,
paper or live.

get_kite_client() is monkeypatched at the module level so no real Kite
Connect credentials or network calls are needed. _instrument_token_cache is
reset between tests since it's module-level state.
"""

import pytest

import trader.kite_client as kite_client
from trader.kite_client import microstructure_checks


class _FakeKite:
    def __init__(self, ltp, upper_circuit=None, lower_circuit=None, turnover_per_day=5_00_00_000):
        self.ltp_value = ltp
        self.upper_circuit = upper_circuit if upper_circuit is not None else ltp * 1.05
        self.lower_circuit = lower_circuit if lower_circuit is not None else ltp * 0.95
        self.turnover_per_day = turnover_per_day  # ₹/day

    def quote(self, key):
        return {
            key: {
                "last_price": self.ltp_value,
                "upper_circuit_limit": self.upper_circuit,
                "lower_circuit_limit": self.lower_circuit,
            }
        }

    def instruments(self, exchange):
        return [{"tradingsymbol": "DUMMY", "instrument_token": 1}]

    def historical_data(self, token, from_date, to_date, interval, **kwargs):
        # 20 days of constant volume/close such that volume*close = turnover_per_day
        volume = int(self.turnover_per_day / self.ltp_value)
        return [{"volume": volume, "close": self.ltp_value}] * 20


@pytest.fixture(autouse=True)
def reset_instrument_cache():
    kite_client._instrument_token_cache = {}
    yield
    kite_client._instrument_token_cache = {}


def mock_kite(monkeypatch, fake):
    monkeypatch.setattr(kite_client, "get_kite_client", lambda: fake)


class TestCircuitBufferCheck:
    def test_rejects_when_near_upper_circuit(self, monkeypatch):
        ltp = 100.0
        fake = _FakeKite(ltp=ltp, upper_circuit=100.5, lower_circuit=80.0)  # 0.5% from upper
        mock_kite(monkeypatch, fake)
        ok, reason = microstructure_checks("DUMMY", capital_to_deploy=10_000)
        assert ok is False
        assert "upper circuit" in reason.lower()

    def test_rejects_when_near_lower_circuit(self, monkeypatch):
        ltp = 100.0
        fake = _FakeKite(ltp=ltp, upper_circuit=120.0, lower_circuit=99.7)  # 0.3% from lower
        mock_kite(monkeypatch, fake)
        ok, reason = microstructure_checks("DUMMY", capital_to_deploy=10_000)
        assert ok is False
        assert "lower circuit" in reason.lower()

    def test_passes_when_comfortably_within_circuit_limits(self, monkeypatch):
        fake = _FakeKite(ltp=100.0, upper_circuit=120.0, lower_circuit=80.0)
        mock_kite(monkeypatch, fake)
        ok, reason = microstructure_checks("DUMMY", capital_to_deploy=10_000)
        assert ok is True


class TestLiquidityCheck:
    def test_rejects_illiquid_stock_below_min_turnover(self, monkeypatch):
        # MIN_DAILY_TURNOVER_INR default ₹3 Cr — give it ₹1 Cr/day
        fake = _FakeKite(ltp=100.0, turnover_per_day=1_00_00_000)
        mock_kite(monkeypatch, fake)
        ok, reason = microstructure_checks("DUMMY", capital_to_deploy=10_000)
        assert ok is False
        assert "illiquid" in reason.lower()

    def test_passes_liquid_stock_above_min_turnover(self, monkeypatch):
        fake = _FakeKite(ltp=100.0, turnover_per_day=10_00_00_000)  # ₹10 Cr/day
        mock_kite(monkeypatch, fake)
        ok, reason = microstructure_checks("DUMMY", capital_to_deploy=10_000)
        assert ok is True


class TestAdvCheck:
    def test_rejects_order_exceeding_adv_cap(self, monkeypatch):
        # MAX_ADV_PCT default 2%. turnover ₹5Cr/day at ltp=100 → avg_daily_volume=500,000 shares.
        # 2% of that = 10,000 shares = ₹1,000,000 notional. Deploy far more than that.
        fake = _FakeKite(ltp=100.0, turnover_per_day=5_00_00_000)
        mock_kite(monkeypatch, fake)
        ok, reason = microstructure_checks("DUMMY", capital_to_deploy=5_000_000)
        assert ok is False
        assert "avg volume" in reason.lower() or "adv" in reason.lower() or "move the market" in reason.lower()

    def test_passes_small_order_within_adv_cap(self, monkeypatch):
        fake = _FakeKite(ltp=100.0, turnover_per_day=5_00_00_000)
        mock_kite(monkeypatch, fake)
        ok, reason = microstructure_checks("DUMMY", capital_to_deploy=10_000)
        assert ok is True


class TestAllChecksPass:
    def test_returns_true_with_passed_message(self, monkeypatch):
        fake = _FakeKite(ltp=100.0, upper_circuit=120.0, lower_circuit=80.0, turnover_per_day=10_00_00_000)
        mock_kite(monkeypatch, fake)
        ok, reason = microstructure_checks("DUMMY", capital_to_deploy=10_000)
        assert ok is True
        assert "passed" in reason.lower()

    def test_quote_fetch_failure_rejects_safely(self, monkeypatch):
        class _BrokenKite:
            def quote(self, key):
                raise ConnectionError("simulated network failure")

        mock_kite(monkeypatch, _BrokenKite())
        ok, reason = microstructure_checks("DUMMY", capital_to_deploy=10_000)
        assert ok is False
        assert "could not fetch quote" in reason.lower()
