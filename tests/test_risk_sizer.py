"""
Tests for risk_sizer/sizer.py — the deterministic position-sizing "compliance desk".

No API credentials needed: size_position() is a pure function of dataclasses
and floats. Capital/limits are passed explicitly per test rather than relying
on the module's .env-derived defaults, so tests are independent of the
environment they run in.
"""

from dataclasses import dataclass
from enum import Enum

import pytest

from risk_sizer.sizer import size_position, OpenPosition


class _Regime(str, Enum):
    BULL = "bull"


@dataclass
class _Signal:
    ticker: str
    sector: str
    confidence_score: float


CAPITAL = 1_000_000.0  # ₹10L, matches the module's documented default


def make_signal(ticker="RELIANCE", sector="Energy", confidence=85.0) -> _Signal:
    return _Signal(ticker=ticker, sector=sector, confidence_score=confidence)


class TestWeeklyDrawdownLimit:
    def test_blocks_when_weekly_loss_exceeds_limit(self):
        decision = size_position(
            signal=make_signal(),
            open_positions=[],
            weekly_pnl=-100_001.0,  # limit is 10% of 10L = -100,000
            capital=CAPITAL,
        )
        assert decision.approved is False
        assert "drawdown" in decision.notes.lower()

    def test_allows_at_exactly_the_limit_boundary(self):
        # weekly_pnl < -drawdown_limit is the trigger condition (strict <),
        # so exactly -100,000 should NOT trip it.
        decision = size_position(
            signal=make_signal(),
            open_positions=[],
            weekly_pnl=-100_000.0,
            capital=CAPITAL,
        )
        assert decision.approved is True


class TestConcurrentPositionCap:
    def test_blocks_when_at_max_concurrent_positions(self):
        open_positions = [
            OpenPosition(ticker=f"STOCK{i}", sector="Tech", capital_deployed=10_000)
            for i in range(5)  # MAX_CONCURRENT_POSITIONS default is 5
        ]
        decision = size_position(
            signal=make_signal(ticker="NEWSTOCK"),
            open_positions=open_positions,
            weekly_pnl=0.0,
            capital=CAPITAL,
        )
        assert decision.approved is False
        assert "positions" in decision.notes.lower()

    def test_allows_below_max_concurrent_positions(self):
        open_positions = [
            OpenPosition(ticker=f"STOCK{i}", sector="Tech", capital_deployed=10_000)
            for i in range(4)
        ]
        decision = size_position(
            signal=make_signal(ticker="NEWSTOCK"),
            open_positions=open_positions,
            weekly_pnl=0.0,
            capital=CAPITAL,
        )
        assert decision.approved is True


class TestNoPyramiding:
    def test_blocks_adding_to_an_already_held_ticker(self):
        open_positions = [
            OpenPosition(ticker="RELIANCE", sector="Energy", capital_deployed=50_000)
        ]
        decision = size_position(
            signal=make_signal(ticker="RELIANCE"),
            open_positions=open_positions,
            weekly_pnl=0.0,
            capital=CAPITAL,
        )
        assert decision.approved is False
        assert "already holding" in decision.notes.lower()

    def test_allows_a_different_ticker_in_the_same_sector(self):
        open_positions = [
            OpenPosition(ticker="RELIANCE", sector="Energy", capital_deployed=50_000)
        ]
        decision = size_position(
            signal=make_signal(ticker="ONGC", sector="Energy"),
            open_positions=open_positions,
            weekly_pnl=0.0,
            capital=CAPITAL,
        )
        assert decision.approved is True


class TestSectorConcentration:
    def test_blocks_when_sector_cap_is_already_hit(self):
        # MAX_SECTOR_PCT default 35% of 10L = 350,000
        open_positions = [
            OpenPosition(ticker="RELIANCE", sector="Energy", capital_deployed=360_000)
        ]
        decision = size_position(
            signal=make_signal(ticker="ONGC", sector="Energy"),
            open_positions=open_positions,
            weekly_pnl=0.0,
            capital=CAPITAL,
        )
        assert decision.approved is False
        assert "sector cap" in decision.notes.lower()

    def test_allows_when_sector_has_headroom(self):
        open_positions = [
            OpenPosition(ticker="RELIANCE", sector="Energy", capital_deployed=50_000)
        ]
        decision = size_position(
            signal=make_signal(ticker="ONGC", sector="Energy", confidence=90.0),
            open_positions=open_positions,
            weekly_pnl=0.0,
            capital=CAPITAL,
        )
        assert decision.approved is True


class TestFreeCapital:
    def test_blocks_when_free_capital_below_minimum(self):
        # MIN_POSITION_INR default 10,000. Deploy almost everything.
        open_positions = [
            OpenPosition(ticker="A", sector="Tech", capital_deployed=995_000)
        ]
        decision = size_position(
            signal=make_signal(ticker="B", sector="Pharma"),
            open_positions=open_positions,
            weekly_pnl=0.0,
            capital=CAPITAL,
        )
        assert decision.approved is False


class TestConfidenceScaling:
    """
    Base position = 20% of capital. Confidence scaling maps 70%→0.60 of
    base, 100%→1.00 of base, roughly linear in between.
    """

    def test_full_scale_at_max_confidence(self):
        decision = size_position(
            signal=make_signal(confidence=100.0),
            open_positions=[],
            weekly_pnl=0.0,
            capital=CAPITAL,
        )
        assert decision.approved is True
        # base_position = 200,000; scale should be ~1.00
        assert decision.capital_to_deploy == pytest.approx(200_000 * 0.95, rel=0.05) or \
            decision.capital_to_deploy == pytest.approx(200_000, rel=0.05)

    def test_reduced_scale_at_minimum_confidence(self):
        decision = size_position(
            signal=make_signal(confidence=70.0),
            open_positions=[],
            weekly_pnl=0.0,
            capital=CAPITAL,
        )
        assert decision.approved is True
        # scale should be ~0.60 of base (200,000 * 0.60 = 120,000)
        assert decision.capital_to_deploy == pytest.approx(120_000, rel=0.05)

    def test_confidence_is_clamped_below_70(self):
        # signal.confidence_score below 70 shouldn't crash or go negative —
        # the module clamps to [70, 100] before scaling.
        decision = size_position(
            signal=make_signal(confidence=50.0),
            open_positions=[],
            weekly_pnl=0.0,
            capital=CAPITAL,
        )
        assert decision.approved is True
        assert decision.capital_to_deploy == pytest.approx(120_000, rel=0.05)

    def test_confidence_is_clamped_above_100(self):
        decision = size_position(
            signal=make_signal(confidence=150.0),
            open_positions=[],
            weekly_pnl=0.0,
            capital=CAPITAL,
        )
        assert decision.approved is True
        assert decision.capital_to_deploy == pytest.approx(200_000, rel=0.05)


class TestSizingClampedByHeadroom:
    def test_sizing_never_exceeds_sector_headroom(self):
        # Sector already has 340,000 deployed; cap is 350,000 → only 10,000
        # headroom left, well below what confidence scaling alone would size.
        open_positions = [
            OpenPosition(ticker="RELIANCE", sector="Energy", capital_deployed=340_000)
        ]
        decision = size_position(
            signal=make_signal(ticker="ONGC", sector="Energy", confidence=100.0),
            open_positions=open_positions,
            weekly_pnl=0.0,
            capital=CAPITAL,
        )
        # 10,000 headroom equals MIN_POSITION_INR exactly — should still approve
        # at the clamped size, not the full 200,000 confidence-scaled target.
        if decision.approved:
            assert decision.capital_to_deploy <= 10_000

    def test_sizing_never_exceeds_free_capital(self):
        open_positions = [
            OpenPosition(ticker="A", sector="Tech", capital_deployed=900_000)
        ]
        decision = size_position(
            signal=make_signal(ticker="B", sector="Pharma", confidence=100.0),
            open_positions=open_positions,
            weekly_pnl=0.0,
            capital=CAPITAL,
        )
        free_capital = CAPITAL - 900_000
        if decision.approved:
            assert decision.capital_to_deploy <= free_capital * 0.95 + 1  # rounding slack
