"""
Drawdown kill switch (2026-10-21): MAX_DRAWDOWN_PCT default 25%, measured on
realised + mark-to-market value, with the peak reset whenever the regime
returns from a cash regime (bear / extreme_fear_crash) to an active one.
Replaces the 8% breaker, which in the 10y backtest froze the system ~83% of
the time (streak_backtests/results/drawdown_study.txt).
"""

import os
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass

import pytest


@pytest.fixture()
def ledger(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    import ledger.db as db
    monkeypatch.setattr(db, "DB_PATH", path)
    schema = os.path.join(os.path.dirname(db.__file__), "schema.sql")
    with closing(sqlite3.connect(path)) as conn, open(schema) as f:
        conn.executescript(f.read())
    yield db
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(path + suffix)
        except OSError:
            pass


@dataclass
class _Reading:
    regime: object
    confidence: float = 70.0
    rationale: str = ""


def _reading(value, confidence=70.0):
    from researcher.regime_classifier import Regime
    return _Reading(regime=Regime(value), confidence=confidence)


def test_default_limit_is_25_pct():
    import re
    import risk_manager.portfolio_risk as pr
    src = open(pr.__file__, encoding="utf-8").read()
    assert re.search(r'os\.getenv\("MAX_DRAWDOWN_PCT",\s*"25\.0"\)', src)
    env = open(os.path.join(os.path.dirname(os.path.dirname(pr.__file__)), ".env.example"), encoding="utf-8").read()
    assert "MAX_DRAWDOWN_PCT=25.0" in env


def test_reset_peak_can_lower_the_peak(ledger):
    assert ledger.get_update_portfolio_peak(1_200_000, "PAPER") == 1_200_000
    ledger.reset_portfolio_peak(900_000, "PAPER")
    assert ledger.get_update_portfolio_peak(950_000, "PAPER") == 950_000  # new high vs reset peak
    assert ledger.get_update_portfolio_peak(1_000_000, "LIVE") == 1_000_000  # other book untouched


def test_regime_state_round_trip(ledger):
    assert ledger.get_last_regime("PAPER") is None
    ledger.set_last_regime("PAPER", "bear")
    ledger.set_last_regime("PAPER", "sideways")
    assert ledger.get_last_regime("PAPER") == "sideways"
    assert ledger.get_last_regime("LIVE") is None


class TestRegimeReturnReset:
    def test_cash_to_active_resets_peak(self, ledger):
        import main
        ledger.get_update_portfolio_peak(1_200_000, "PAPER")
        ledger.set_last_regime("PAPER", "bear")
        assert main._regime_return_peak_reset("PAPER", _reading("sideways"), 880_000) is True
        assert ledger.get_update_portfolio_peak(880_000, "PAPER") == 880_000
        assert ledger.get_last_regime("PAPER") == "sideways"

    def test_crash_to_bull_resets(self, ledger):
        import main
        ledger.set_last_regime("PAPER", "extreme_fear_crash")
        assert main._regime_return_peak_reset("PAPER", _reading("bull"), 900_000) is True

    @pytest.mark.parametrize("prev,now", [("bull", "sideways"), ("bear", "extreme_fear_crash"),
                                          ("sideways", "bear"), (None, "bull")])
    def test_no_reset_otherwise(self, ledger, prev, now):
        import main
        ledger.get_update_portfolio_peak(1_200_000, "PAPER")
        if prev:
            ledger.set_last_regime("PAPER", prev)
        assert main._regime_return_peak_reset("PAPER", _reading(now), 900_000) is False
        assert ledger.get_update_portfolio_peak(900_000, "PAPER") == 1_200_000

    def test_degraded_read_is_ignored(self, ledger):
        import main
        ledger.set_last_regime("PAPER", "bear")
        assert main._regime_return_peak_reset("PAPER", _reading("sideways", confidence=0.0), 900_000) is False
        assert ledger.get_last_regime("PAPER") == "bear"  # not recorded


class TestGate:
    def test_halt_kind_set(self, monkeypatch):
        import ledger.db as db
        import risk_manager.portfolio_risk as pr
        monkeypatch.setattr(pr, "CAPITAL", 1_000_000.0)
        monkeypatch.setattr(pr, "MAX_DRAWDOWN_PCT", 25.0)
        monkeypatch.setattr(pr, "_send_halt_alert", lambda s: None)
        monkeypatch.setattr(db, "get_all_time_pnl", lambda mode: -300_000.0)
        monkeypatch.setattr(db, "get_update_portfolio_peak", lambda v, mode: max(1_000_000.0, v))
        s = pr.check_portfolio_risk([], 0.0, mode="PAPER")
        assert not s.approved and s.halt_kind == "DRAWDOWN" and "KILL SWITCH" in s.halt_reason
        monkeypatch.setattr(db, "get_all_time_pnl", lambda mode: -200_000.0)  # -20%: normal, keep trading
        s = pr.check_portfolio_risk([], 0.0, mode="PAPER")
        assert s.approved and s.halt_kind == ""


def test_halted_cycle_unhalts_when_regime_returns(ledger, monkeypatch):
    """End to end through run_cycle: halted at -30%, regime goes bear -> sideways,
    peak resets, the gate re-runs and passes, and the cycle carries on with the
    reading it already took (no second classify)."""
    import main
    import risk_manager.portfolio_risk as pr
    import researcher.regime_classifier as rc
    import researcher.signal_generator as sg

    monkeypatch.setattr(main, "PAPER_MODE", True, raising=False)
    monkeypatch.setattr(pr, "CAPITAL", 1_000_000.0)
    monkeypatch.setattr(pr, "MAX_DRAWDOWN_PCT", 25.0)
    monkeypatch.setattr(pr, "_send_halt_alert", lambda s: None)
    monkeypatch.setattr(ledger, "get_all_time_pnl", lambda mode: -300_000.0)
    monkeypatch.setattr(ledger, "get_open_positions", lambda mode=None: [])
    monkeypatch.setattr(ledger, "get_weekly_pnl", lambda mode=None: 0.0)
    import universe.loader as ul
    monkeypatch.setattr(ul, "load_scan_universe", lambda: [])
    ledger.get_update_portfolio_peak(1_000_000.0, "PAPER")
    ledger.set_last_regime("PAPER", "bear")

    calls = []
    monkeypatch.setattr(rc, "classify_regime", lambda *a, **k: calls.append(1) or _reading("sideways"))
    reached = []
    monkeypatch.setattr(sg, "generate_signal", lambda *a, **k: reached.append(1) or None)

    main.run_cycle()

    assert len(calls) == 1
    assert reached == [1], "after the peak reset the cycle must proceed to signal generation"
    assert ledger.get_last_regime("PAPER") == "sideways"


def test_halted_cycle_stays_halted_in_bear(ledger, monkeypatch):
    import main
    import risk_manager.portfolio_risk as pr
    import researcher.regime_classifier as rc
    import researcher.signal_generator as sg

    monkeypatch.setattr(main, "PAPER_MODE", True, raising=False)
    monkeypatch.setattr(pr, "CAPITAL", 1_000_000.0)
    monkeypatch.setattr(pr, "MAX_DRAWDOWN_PCT", 25.0)
    monkeypatch.setattr(pr, "_send_halt_alert", lambda s: None)
    monkeypatch.setattr(ledger, "get_all_time_pnl", lambda mode: -300_000.0)
    monkeypatch.setattr(ledger, "get_open_positions", lambda mode=None: [])
    monkeypatch.setattr(ledger, "get_weekly_pnl", lambda mode=None: 0.0)
    import universe.loader as ul
    monkeypatch.setattr(ul, "load_scan_universe", lambda: [])
    ledger.get_update_portfolio_peak(1_000_000.0, "PAPER")
    ledger.set_last_regime("PAPER", "bear")
    monkeypatch.setattr(rc, "classify_regime", lambda *a, **k: _reading("bear"))
    reached = []
    monkeypatch.setattr(sg, "generate_signal", lambda *a, **k: reached.append(1) or None)

    main.run_cycle()
    assert reached == []
