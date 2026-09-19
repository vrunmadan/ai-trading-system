"""
Tests for risk_manager/portfolio_risk.py — the portfolio-level circuit breakers
that run before every research cycle.

check_portfolio_risk(, mode="PAPER") reads CAPITAL/MAX_*_PCT module constants (set once at
import time from .env) and calls ledger.db.get_all_time_pnl() /
get_update_portfolio_peak() internally. Both are monkeypatched per test so
these tests are deterministic regardless of the environment or any real DB.
"""

from dataclasses import dataclass

import pytest

import ledger.db as ledger_db
import risk_manager.portfolio_risk as portfolio_risk
from risk_manager.portfolio_risk import check_portfolio_risk


@dataclass
class _OpenPosition:
    ticker: str
    sector: str
    capital_deployed: float
    market_value: float | None = None  # None = no live price this cycle (2026-09-19 review, item 4)


@pytest.fixture(autouse=True)
def fixed_thresholds(monkeypatch):
    """Pin every threshold to a known value so tests don't depend on .env."""
    monkeypatch.setattr(portfolio_risk, "CAPITAL", 1_000_000.0)
    monkeypatch.setattr(portfolio_risk, "MAX_DRAWDOWN_PCT", 8.0)
    monkeypatch.setattr(portfolio_risk, "MAX_DEPLOYED_PCT", 65.0)
    monkeypatch.setattr(portfolio_risk, "WEEKLY_LOSS_LIMIT", -15_000.0)
    monkeypatch.setattr(portfolio_risk, "MAX_OPEN_POSITIONS", 6)
    monkeypatch.setattr(portfolio_risk, "MAX_SECTOR_PCT", 30.0)


def mock_ledger(monkeypatch, all_time_pnl=0.0, peak_value=None):
    """
    get_update_portfolio_peak(current_value, mode) returns the higher of the
    stored peak and current_value — mimic that behaviour so drawdown tests can
    control the peak independently of the current portfolio value. Both real
    functions now require mode ("PAPER"/"LIVE") so a live drawdown is never
    measured against a paper peak or vice versa; these tests don't care which
    book, so the mocks just accept and ignore it.
    """
    monkeypatch.setattr(ledger_db, "get_all_time_pnl", lambda mode: all_time_pnl)
    effective_peak = peak_value if peak_value is not None else (1_000_000.0 + all_time_pnl)
    monkeypatch.setattr(
        ledger_db, "get_update_portfolio_peak",
        lambda current_value, mode: max(effective_peak, current_value),
    )


class TestDrawdownCircuitBreaker:
    def test_halts_when_drawdown_exceeds_limit(self, monkeypatch):
        # Peak was ₹1,000,000; current value ₹900,000 → -10% drawdown, past -8% limit
        mock_ledger(monkeypatch, all_time_pnl=-100_000.0, peak_value=1_000_000.0)
        status = check_portfolio_risk(open_positions=[], weekly_pnl=0.0, mode="PAPER")
        assert status.approved is False
        assert "DRAWDOWN" in status.halt_reason

    def test_allows_when_drawdown_within_limit(self, monkeypatch):
        # -5% drawdown, within the -8% limit
        mock_ledger(monkeypatch, all_time_pnl=-50_000.0, peak_value=1_000_000.0)
        status = check_portfolio_risk(open_positions=[], weekly_pnl=0.0, mode="PAPER")
        assert status.approved is True


class TestWeeklyLossCircuitBreaker:
    def test_halts_when_weekly_loss_exceeds_limit(self, monkeypatch):
        mock_ledger(monkeypatch, all_time_pnl=0.0)
        status = check_portfolio_risk(open_positions=[], weekly_pnl=-15_001.0, mode="PAPER")
        assert status.approved is False
        assert "WEEKLY LOSS" in status.halt_reason

    def test_allows_at_exactly_the_limit(self, monkeypatch):
        mock_ledger(monkeypatch, all_time_pnl=0.0)
        # weekly_pnl <= WEEKLY_LOSS_LIMIT is the trigger — exactly -15,000 DOES trip it
        status = check_portfolio_risk(open_positions=[], weekly_pnl=-15_000.0, mode="PAPER")
        assert status.approved is False


class TestDeployedCapitalCircuitBreaker:
    def test_exposure_is_advisory_not_a_halt(self, monkeypatch):
        # Exposure is now ADVISORY (user directive 2026-08-26): the cycle is NOT
        # halted, but the breach is surfaced in advisory_flags so it reaches the
        # user on the alert.
        mock_ledger(monkeypatch, all_time_pnl=0.0)
        open_positions = [_OpenPosition("A", "Tech", 650_000.0)]  # exactly 65%
        status = check_portfolio_risk(open_positions=open_positions, weekly_pnl=0.0, mode="PAPER")
        assert status.approved is True
        assert status.halt_reason == ""
        assert any("EXPOSURE" in f for f in status.advisory_flags)

    def test_allows_below_deployed_limit(self, monkeypatch):
        mock_ledger(monkeypatch, all_time_pnl=0.0)
        # Spread across sectors (30% cap each) so this exercises ONLY the
        # deployed-capital check, not the sector-concentration one.
        open_positions = [
            _OpenPosition("A", "Tech", 200_000.0),
            _OpenPosition("B", "Pharma", 200_000.0),
            _OpenPosition("C", "Energy", 200_000.0),
        ]  # 60% deployed total, 20% per sector
        status = check_portfolio_risk(open_positions=open_positions, weekly_pnl=0.0, mode="PAPER")
        assert status.approved is True


class TestPositionCountCircuitBreaker:
    def test_position_count_is_advisory_not_a_halt(self, monkeypatch):
        # Position-count limit is advisory now: flagged, not halted.
        mock_ledger(monkeypatch, all_time_pnl=0.0)
        open_positions = [_OpenPosition(f"S{i}", "Tech", 10_000.0) for i in range(6)]
        status = check_portfolio_risk(open_positions=open_positions, weekly_pnl=0.0, mode="PAPER")
        assert status.approved is True
        assert any("POSITION COUNT" in f for f in status.advisory_flags)

    def test_allows_below_max_open_positions(self, monkeypatch):
        mock_ledger(monkeypatch, all_time_pnl=0.0)
        open_positions = [_OpenPosition(f"S{i}", "Tech", 10_000.0) for i in range(5)]
        status = check_portfolio_risk(open_positions=open_positions, weekly_pnl=0.0, mode="PAPER")
        assert status.approved is True


class TestSectorConcentrationCircuitBreaker:
    def test_sector_concentration_is_advisory_not_a_halt(self, monkeypatch):
        # Sector concentration is advisory now: flagged, not halted.
        mock_ledger(monkeypatch, all_time_pnl=0.0)
        open_positions = [_OpenPosition("A", "Energy", 310_000.0)]  # 31% > 30% cap
        status = check_portfolio_risk(open_positions=open_positions, weekly_pnl=0.0, mode="PAPER")
        assert status.approved is True
        assert any("SECTOR CONCENTRATION" in f for f in status.advisory_flags)
        assert any("Energy" in f for f in status.advisory_flags)

    def test_allows_multi_sector_below_cap(self, monkeypatch):
        mock_ledger(monkeypatch, all_time_pnl=0.0)
        open_positions = [
            _OpenPosition("A", "Energy", 200_000.0),
            _OpenPosition("B", "Tech", 200_000.0),
        ]
        status = check_portfolio_risk(open_positions=open_positions, weekly_pnl=0.0, mode="PAPER")
        assert status.approved is True


class TestBreakerPriorityOrder:
    def test_drawdown_takes_priority_over_weekly_loss(self, monkeypatch):
        # Trip both simultaneously — drawdown is checked first in the source.
        mock_ledger(monkeypatch, all_time_pnl=-150_000.0, peak_value=1_000_000.0)
        status = check_portfolio_risk(open_positions=[], weekly_pnl=-20_000.0, mode="PAPER")
        assert status.approved is False
        assert "DRAWDOWN" in status.halt_reason
        assert "WEEKLY LOSS" not in status.halt_reason


class TestPeakTracking:
    def test_peak_never_decreases(self, monkeypatch):
        # get_update_portfolio_peak is mocked to mimic max(stored_peak, current) —
        # verify check_portfolio_risk surfaces that value, not the current one.
        mock_ledger(monkeypatch, all_time_pnl=-50_000.0, peak_value=1_200_000.0)
        status = check_portfolio_risk(open_positions=[], weekly_pnl=0.0, mode="PAPER")
        assert status.peak_value == 1_200_000.0
        assert status.portfolio_value == 950_000.0


class TestMarkToMarketEquity:
    """
    2026-09-19 review, item 4: portfolio equity was configured capital plus
    realised P&L only — open (unrealised) losses and gains on positions still
    held were invisible to the drawdown circuit breaker. A ₹10L account with
    ₹6L invested and those holdings down 20% had lost ₹1.2L that the old
    calculation could not see at all.
    """

    def test_unrealised_loss_on_open_positions_counts_toward_drawdown(self, monkeypatch):
        # Zero realised P&L — the OLD code would have shown 0% drawdown here.
        mock_ledger(monkeypatch, all_time_pnl=0.0, peak_value=1_000_000.0)
        # A ₹600,000 position (at cost) now worth ₹480,000 — a ₹120,000 open loss.
        open_positions = [_OpenPosition("A", "Tech", 600_000.0, market_value=480_000.0)]
        status = check_portfolio_risk(open_positions=open_positions, weekly_pnl=0.0, mode="PAPER")
        assert status.unrealized_pnl == pytest.approx(-120_000.0)
        assert status.portfolio_value == pytest.approx(880_000.0)
        assert status.drawdown_pct == pytest.approx(-12.0)
        assert status.approved is False
        assert "DRAWDOWN" in status.halt_reason

    def test_unrealised_gain_on_open_positions_raises_portfolio_value(self, monkeypatch):
        mock_ledger(monkeypatch, all_time_pnl=0.0, peak_value=1_000_000.0)
        open_positions = [_OpenPosition("A", "Tech", 200_000.0, market_value=250_000.0)]
        status = check_portfolio_risk(open_positions=open_positions, weekly_pnl=0.0, mode="PAPER")
        assert status.unrealized_pnl == pytest.approx(50_000.0)
        assert status.portfolio_value == pytest.approx(1_050_000.0)
        assert status.approved is True

    def test_a_position_with_no_fetchable_price_is_treated_as_flat_not_excluded(self, monkeypatch):
        """
        market_value=None (a live LTP could not be fetched this cycle) must
        not raise, and must not be treated as a ₹0 position — it simply
        contributes no unrealised swing, the same as before this feature
        existed.
        """
        mock_ledger(monkeypatch, all_time_pnl=0.0, peak_value=1_000_000.0)
        open_positions = [_OpenPosition("A", "Tech", 300_000.0, market_value=None)]
        status = check_portfolio_risk(open_positions=open_positions, weekly_pnl=0.0, mode="PAPER")
        assert status.unrealized_pnl == 0.0
        assert status.portfolio_value == pytest.approx(1_000_000.0)

    def test_positions_without_a_market_value_attribute_at_all_still_work(self, monkeypatch):
        """
        Backward compatibility: an OpenPosition-like object predating this
        field (no market_value attribute at all) must not crash
        check_portfolio_risk — it is treated the same as market_value=None.
        """
        @dataclass
        class _LegacyOpenPosition:
            ticker: str
            sector: str
            capital_deployed: float

        mock_ledger(monkeypatch, all_time_pnl=0.0, peak_value=1_000_000.0)
        open_positions = [_LegacyOpenPosition("A", "Tech", 300_000.0)]
        status = check_portfolio_risk(open_positions=open_positions, weekly_pnl=0.0, mode="PAPER")
        assert status.unrealized_pnl == 0.0
        assert status.portfolio_value == pytest.approx(1_000_000.0)
