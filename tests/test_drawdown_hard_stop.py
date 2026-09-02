"""
The weekly-drawdown hard stop used to `return` with only a log line — a
candidate that cleared the Researcher's 75% bar and was killed by the
portfolio circuit breaker left zero trace: no signals row, no alert, no
shadow-check eligibility. The alert templates already described "weekly
drawdown" as a DROPPED_SIZER cause (gmail_alert._DROP_STAGE_BLURB), so the
code was out of sync with its own design. This covers the fix: the drop is
now logged (with price, when available) and alerted, same as every other
pre-QC drop stage.
"""

import os
import sqlite3
import tempfile

import pytest


@pytest.fixture()
def ledger(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    import ledger.db as db

    monkeypatch.setattr(db, "DB_PATH", path)
    schema = os.path.join(os.path.dirname(db.__file__), "schema.sql")
    with sqlite3.connect(path) as conn:
        with open(schema) as f:
            conn.executescript(f.read())
        conn.commit()

    yield db

    try:
        os.unlink(path)
    except OSError:
        pass


@pytest.fixture()
def signal():
    from researcher.regime_classifier import Regime
    from researcher.signal_generator import TradeSignal

    return TradeSignal(
        ticker="TITAN", sector="CONSUMER GOODS", exchange="NSE",
        regime=Regime.BULL, strategy_bucket="52wk_breakout", direction="BUY",
        technical_score=88.0, fundamental_score=60.0, confidence_score=78.0,
        rationale="Breakout on 1.9x volume.",
    )


def _run_cycle_with_drawdown_halt(monkeypatch, signal, ltp=450.0, ltp_raises=False):
    """Drive run_cycle up to the sizer's hard stop, with a drawdown-triggered
    rejection, and everything else stubbed out."""
    import main

    monkeypatch.setattr(main, "PAPER_MODE", True, raising=False)

    import ledger.db as db
    monkeypatch.setattr(db, "get_open_positions", lambda: [])
    monkeypatch.setattr(db, "get_weekly_pnl", lambda: -50_000.0)

    import universe.loader as ul
    monkeypatch.setattr(ul, "load_universe", lambda: [])

    import risk_manager.portfolio_risk as pr

    class PortfolioStatus:
        approved = True
        halt_reason = ""
        advisory_flags = []

    monkeypatch.setattr(pr, "check_portfolio_risk", lambda *a, **k: PortfolioStatus())

    import researcher.regime_classifier as rc
    from researcher.regime_classifier import Regime

    class Reading:
        regime = Regime.BULL
        confidence = 80.0
        rationale = "bull"

    monkeypatch.setattr(rc, "classify_regime", lambda *a, **k: Reading())

    import researcher.signal_generator as sg
    monkeypatch.setattr(sg, "generate_signal", lambda *a, **k: signal)

    import risk_sizer.sizer as sz
    from risk_sizer.sizer import SizingDecision

    monkeypatch.setattr(
        sz, "size_position",
        lambda **k: SizingDecision(
            approved=False, capital_to_deploy=0.0, quantity=0,
            notes="Weekly drawdown breached: -6.2% this week (limit -5.0%).",
        ),
    )

    import trader.kite_client as kc
    if ltp_raises:
        def _boom(*a, **k):
            raise RuntimeError("Kite session expired")
        monkeypatch.setattr(kc, "get_ltp", _boom)
    else:
        monkeypatch.setattr(kc, "get_ltp", lambda *a, **k: ltp)

    sent_alerts = []
    import alerts.gmail_alert as ga
    monkeypatch.setattr(
        ga, "send_candidate_dropped_alert",
        lambda *a, **k: sent_alerts.append((a, k)) or True,
    )

    main.run_cycle()
    return sent_alerts


def test_drawdown_halt_is_logged_with_price_and_alerted(ledger, signal, monkeypatch):
    sent_alerts = _run_cycle_with_drawdown_halt(monkeypatch, signal, ltp=450.0)

    with ledger.get_db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT ticker, status, price_at_signal FROM signals")]

    assert len(rows) == 1, "the candidate must leave a trace, not vanish"
    assert rows[0]["ticker"] == "TITAN"
    assert rows[0]["status"] == "DROPPED_SIZER"
    assert rows[0]["price_at_signal"] == 450.0
    assert len(sent_alerts) == 1, "a drawdown-halted candidate is still a heads-up, not silence"


def test_drawdown_halt_is_still_logged_if_the_price_fetch_fails(ledger, signal, monkeypatch):
    """The whole point is not to vanish — even if we can't get a price, the
    drop itself must still be recorded and alerted."""
    _run_cycle_with_drawdown_halt(monkeypatch, signal, ltp_raises=True)

    with ledger.get_db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT status, price_at_signal FROM signals")]

    assert len(rows) == 1
    assert rows[0]["status"] == "DROPPED_SIZER"
    assert rows[0]["price_at_signal"] is None


def test_drawdown_halted_signal_is_shadow_check_eligible(ledger, signal, monkeypatch):
    _run_cycle_with_drawdown_halt(monkeypatch, signal, ltp=450.0)

    # Backdate it so it's old enough for a 1-day shadow check.
    with ledger.get_db() as conn:
        conn.execute(
            "UPDATE signals SET created_at = datetime(created_at, '-2 days')"
        )

    due = ledger.get_signals_needing_shadow_check(horizon_days=1)
    assert len(due) == 1
    assert due[0]["ticker"] == "TITAN"
