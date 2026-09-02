"""
Shadow price checks: grading QC_BLOCKED / QC_ERROR signals against what the
price actually did, without ever placing an order.

Covers:
  - get_signals_needing_shadow_check only surfaces due, unpriced-yet,
    price-known candidates (not AGREE signals, not ones missing a price,
    not ones already checked at that horizon, not ones too old to trust).
  - record_shadow_check is idempotent per (signal_id, horizon_days).
  - run_shadow_checks() computes a direction-adjusted return and only
    checks the horizons that have actually elapsed.
"""

import os
import sqlite3
import tempfile
from datetime import datetime, timedelta

import pytest
import pytz

IST = pytz.timezone("Asia/Kolkata")


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


def _insert_signal(db, ticker, status, days_ago, price_at_signal=100.0,
                    direction="BUY", qc_verdict="DISAGREE"):
    created_at = (datetime.now(IST) - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
    with db.get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO signals (
                created_at, ticker, exchange, regime, strategy_bucket, direction,
                technical_score, fundamental_score, confidence_score,
                researcher_rationale, qc_verdict, qc_rationale, status, price_at_signal
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (created_at, ticker, "NSE", "bull", "52wk_breakout", direction,
             90.0, 60.0, 78.0, "r", qc_verdict, "qc says no", status, price_at_signal),
        )
        return cur.lastrowid


# ---------------------------------------------------------------------------
# get_signals_needing_shadow_check
# ---------------------------------------------------------------------------

def test_finds_a_due_blocked_signal_with_a_known_price(ledger):
    _insert_signal(ledger, "HEG", "QC_BLOCKED", days_ago=2)
    due = ledger.get_signals_needing_shadow_check(horizon_days=1)
    assert len(due) == 1
    assert due[0]["ticker"] == "HEG"


def test_excludes_signals_not_old_enough_yet(ledger):
    _insert_signal(ledger, "HEG", "QC_BLOCKED", days_ago=1)
    due = ledger.get_signals_needing_shadow_check(horizon_days=5)
    assert due == []


def test_excludes_agree_and_pending_signals(ledger):
    _insert_signal(ledger, "TITAN", "PENDING", days_ago=2, qc_verdict="AGREE")
    _insert_signal(ledger, "TITAN", "EXECUTED", days_ago=2, qc_verdict="AGREE")
    due = ledger.get_signals_needing_shadow_check(horizon_days=1)
    assert due == []


def test_includes_no_response_signals(ledger):
    """
    QC agreeing and the alert going out isn't the end of the story if
    nobody clicked it — that's just as gradeable a "no trade happened" as a
    QC block, for a different reason (unanswered, not refused).
    """
    _insert_signal(ledger, "REDINGTON", "NO_RESPONSE", days_ago=2, qc_verdict="AGREE")
    due = ledger.get_signals_needing_shadow_check(horizon_days=1)
    assert len(due) == 1
    assert due[0]["ticker"] == "REDINGTON"


def test_excludes_rejected_and_not_executed_signals(ledger):
    """
    REJECTED is a decision, NOT_EXECUTED is an execution-layer outcome —
    deliberately out of scope for now (see SHADOW_CHECK_STATUSES).
    """
    _insert_signal(ledger, "TITAN", "REJECTED", days_ago=2, qc_verdict="AGREE")
    _insert_signal(ledger, "TITAN", "NOT_EXECUTED", days_ago=2, qc_verdict="AGREE")
    due = ledger.get_signals_needing_shadow_check(horizon_days=1)
    assert due == []


def test_excludes_signals_with_no_recorded_price(ledger):
    _insert_signal(ledger, "HEG", "QC_BLOCKED", days_ago=2, price_at_signal=None)
    due = ledger.get_signals_needing_shadow_check(horizon_days=1)
    assert due == []


def test_excludes_signals_too_old_to_trust(ledger):
    """A gap in the scheduler shouldn't cause a months-old block to be graded
    against today's price as if that were a real N-day-later reading."""
    _insert_signal(ledger, "HEG", "QC_BLOCKED", days_ago=30)
    due = ledger.get_signals_needing_shadow_check(horizon_days=5, max_age_days=14)
    assert due == []


def test_excludes_signals_already_checked_at_that_horizon(ledger):
    signal_id = _insert_signal(ledger, "HEG", "QC_BLOCKED", days_ago=2)
    ledger.record_shadow_check(signal_id, horizon_days=1, price_at_check=110.0, return_pct=10.0)
    due = ledger.get_signals_needing_shadow_check(horizon_days=1)
    assert due == []
    # A different, not-yet-recorded horizon still shows up once it's due.
    due5 = ledger.get_signals_needing_shadow_check(horizon_days=5)
    assert due5 == []  # not old enough for 5d yet (only 2 days old)


# ---------------------------------------------------------------------------
# record_shadow_check
# ---------------------------------------------------------------------------

def test_record_shadow_check_is_idempotent_per_signal_and_horizon(ledger):
    signal_id = _insert_signal(ledger, "HEG", "QC_BLOCKED", days_ago=2)
    ledger.record_shadow_check(signal_id, horizon_days=1, price_at_check=110.0, return_pct=10.0)
    ledger.record_shadow_check(signal_id, horizon_days=1, price_at_check=999.0, return_pct=999.0)

    rows = ledger.get_shadow_checks_for_signal_ids([signal_id])
    assert len(rows) == 1
    assert rows[0]["return_pct"] == 10.0  # first write wins, no silent overwrite


def test_different_horizons_both_recorded(ledger):
    signal_id = _insert_signal(ledger, "HEG", "QC_BLOCKED", days_ago=6)
    ledger.record_shadow_check(signal_id, horizon_days=1, price_at_check=105.0, return_pct=5.0)
    ledger.record_shadow_check(signal_id, horizon_days=5, price_at_check=120.0, return_pct=20.0)

    rows = ledger.get_shadow_checks_for_signal_ids([signal_id])
    assert [r["horizon_days"] for r in rows] == [1, 5]


# ---------------------------------------------------------------------------
# run_shadow_checks — end to end, LTP stubbed
# ---------------------------------------------------------------------------

def test_run_shadow_checks_computes_return_for_a_buy(ledger, monkeypatch):
    import trader.kite_client as kc
    monkeypatch.setattr(kc, "get_ltp", lambda ticker, exchange="NSE": 110.0)

    signal_id = _insert_signal(ledger, "HEG", "QC_BLOCKED", days_ago=2,
                                price_at_signal=100.0, direction="BUY")

    from auditor.shadow_tracker import run_shadow_checks
    recorded = run_shadow_checks()

    # Only the 1-day horizon has elapsed for a 2-day-old signal.
    assert recorded == 1
    rows = ledger.get_shadow_checks_for_signal_ids([signal_id])
    assert len(rows) == 1
    assert rows[0]["horizon_days"] == 1
    assert rows[0]["return_pct"] == pytest.approx(10.0)


def test_run_shadow_checks_covers_a_no_response_signal(ledger, monkeypatch):
    """The Redington case: QC agreed, the alert went out, nobody answered.
    That's still worth grading — same mechanism as a QC_BLOCKED signal."""
    import trader.kite_client as kc
    monkeypatch.setattr(kc, "get_ltp", lambda ticker, exchange="NSE": 121.0)

    signal_id = _insert_signal(ledger, "REDINGTON", "NO_RESPONSE", days_ago=2,
                                price_at_signal=110.0, direction="BUY", qc_verdict="AGREE")

    from auditor.shadow_tracker import run_shadow_checks
    recorded = run_shadow_checks()

    assert recorded == 1
    rows = ledger.get_shadow_checks_for_signal_ids([signal_id])
    assert rows[0]["return_pct"] == pytest.approx(10.0)


def test_run_shadow_checks_flips_sign_for_a_sell(ledger, monkeypatch):
    import trader.kite_client as kc
    monkeypatch.setattr(kc, "get_ltp", lambda ticker, exchange="NSE": 90.0)

    signal_id = _insert_signal(ledger, "HEG", "QC_BLOCKED", days_ago=2,
                                price_at_signal=100.0, direction="SELL")

    from auditor.shadow_tracker import run_shadow_checks
    run_shadow_checks()

    rows = ledger.get_shadow_checks_for_signal_ids([signal_id])
    # Price fell 10% — a win for a SELL, so the graded return is positive.
    assert rows[0]["return_pct"] == pytest.approx(10.0)


def test_run_shadow_checks_is_safe_to_call_daily(ledger, monkeypatch):
    """Calling it again the same day must not double-record or error."""
    import trader.kite_client as kc
    monkeypatch.setattr(kc, "get_ltp", lambda ticker, exchange="NSE": 110.0)

    _insert_signal(ledger, "HEG", "QC_BLOCKED", days_ago=6, price_at_signal=100.0)

    from auditor.shadow_tracker import run_shadow_checks
    first = run_shadow_checks()
    second = run_shadow_checks()

    assert first == 3   # 1d, 3d, and 5d all due for a 6-day-old signal
    assert second == 0  # nothing new left to check


def test_run_shadow_checks_skips_a_ticker_whose_ltp_fetch_fails(ledger, monkeypatch):
    import trader.kite_client as kc

    def _boom(ticker, exchange="NSE"):
        raise RuntimeError("Kite session expired")

    monkeypatch.setattr(kc, "get_ltp", _boom)
    _insert_signal(ledger, "HEG", "QC_BLOCKED", days_ago=2, price_at_signal=100.0)

    from auditor.shadow_tracker import run_shadow_checks
    recorded = run_shadow_checks()  # must not raise

    assert recorded == 0
