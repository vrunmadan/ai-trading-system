"""
Guards at the approval boundary.

Two defects lived here, both found by the 2026-08-21 review and both
reproduced against a real ledger before being fixed:

1. The double-approve guard fetched the OLDEST trade row and tested it for
   NOT_EXECUTED. Once any attempt was written off, the guard consulted that
   dead row forever, so every further tap of the never-expiring approve link
   wrote another PENDING trade. One signal with one intended Rs 1,80,000
   position became Rs 5,40,000 of recorded exposure after three taps, and it
   was unbounded. Phantom exposure trips the portfolio circuit breakers,
   halting real trading over positions that were never placed.

2. handle_email_action never read signals.status, so approval was gated on
   share quantity alone. A signal QC had adversarially reviewed and REFUSED
   would still open a pre-filled Kite basket. The QC gate was a property of
   one code path in run_cycle rather than a property of the data.
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
        with open(schema, encoding="utf-8") as f:
            conn.executescript(f.read())
        conn.commit()

    yield db

    try:
        os.unlink(path)
    except OSError:
        pass


@pytest.fixture()
def approve(monkeypatch):
    monkeypatch.setenv("APPROVAL_SECRET", "s")
    monkeypatch.setenv("KITE_API_KEY", "k")
    monkeypatch.setenv("PAPER_MODE", "false")
    import alerts.gmail_alert as ga
    monkeypatch.setattr(ga, "send_plain_email", lambda **kw: True)
    return ga.handle_email_action


def _signal(db, status="PENDING", quantity=400, capital=180_000.0):
    with db.get_db() as conn:
        return conn.execute(
            "INSERT INTO signals (created_at,ticker,exchange,regime,strategy_bucket,"
            "direction,technical_score,fundamental_score,confidence_score,"
            "researcher_rationale,sized_quantity,capital_to_deploy,sizer_notes,status)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (db.now_ist(), "ACME", "NSE", "bull", "52wk_breakout", "BUY",
             80.0, 70.0, 84.0, "x", quantity, capital, "x", status),
        ).lastrowid


def _exposure(db):
    ps = db.get_open_positions()
    return len(ps), sum(p["entry_price"] * p["quantity"] for p in ps)


def _write_off(db, monkeypatch):
    """Reconcile with an empty Kite so the pending trade becomes NOT_EXECUTED."""
    class NoKite:
        def positions(self): return {"net": []}
        def holdings(self): return []

    import trader.kite_client as kc
    monkeypatch.setattr(kc, "get_kite_client", lambda: NoKite())
    monkeypatch.setattr("monitor.trade_reconciler.MAX_PENDING_AGE_DAYS", 0)
    from monitor.trade_reconciler import reconcile_pending_trades
    reconcile_pending_trades()


# ---------------------------------------------------------------------------
# 1. The double-approve guard
# ---------------------------------------------------------------------------

def test_repeated_taps_do_not_inflate_exposure(ledger, approve, monkeypatch):
    """The reproduction from the review, as a regression test."""
    db = ledger
    sid = _signal(db)

    approve("approve", sid)
    assert _exposure(db) == (1, 180_000.0)

    _write_off(db, monkeypatch)
    assert _exposure(db) == (0, 0)

    for _ in range(3):
        approve("approve", sid)
        count, rupees = _exposure(db)
        assert count == 1, "a repeated tap must not add another open position"
        assert rupees == 180_000.0, (
            f"one signal, one intended position, but exposure reached "
            f"Rs {rupees:,.0f}"
        )


def test_a_written_off_order_may_be_retried_exactly_once(ledger, approve, monkeypatch):
    """
    NOT_EXECUTED means the order never reached the market, so a deliberate
    retry is legitimate. It must produce one new trade, not one per tap.
    """
    db = ledger
    sid = _signal(db)
    approve("approve", sid)
    _write_off(db, monkeypatch)

    approve("approve", sid)
    approve("approve", sid)
    approve("approve", sid)

    with db.get_db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT fill_status FROM trades WHERE signal_id=? ORDER BY id", (sid,))]
    assert [r["fill_status"] for r in rows] == ["NOT_EXECUTED", "PENDING"]


def test_second_tap_reopens_the_same_basket(ledger, approve):
    db = ledger
    sid = _signal(db)
    approve("approve", sid)
    msg, ok, url = approve("approve", sid)

    assert ok is True and url is not None
    assert "already approved" in msg.lower()
    assert _exposure(db) == (1, 180_000.0)


def test_live_trade_lookup_ignores_written_off_rows(ledger, approve, monkeypatch):
    db = ledger
    sid = _signal(db)
    approve("approve", sid)
    _write_off(db, monkeypatch)

    assert db.get_live_trade_for_signal(sid) is None
    # the row still exists for the audit trail
    assert db.get_trade_for_signal(sid)["fill_status"] == "NOT_EXECUTED"


def test_trade_lookup_returns_the_current_row_not_the_first(
    ledger, approve, monkeypatch
):
    """It ordered oldest-first, which is what disabled the guard."""
    db = ledger
    sid = _signal(db)
    approve("approve", sid)
    _write_off(db, monkeypatch)
    approve("approve", sid)

    current = db.get_trade_for_signal(sid)
    assert current["fill_status"] == "PENDING"
    assert current["id"] == 2


# ---------------------------------------------------------------------------
# 2. The pipeline's verdict is enforced here, not only in run_cycle
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", [
    "QC_BLOCKED", "QC_ERROR", "REJECTED", "NO_RESPONSE",
    "SKIPPED", "EXECUTED", "DROPPED_SIZER", "DROPPED_NO_PRICE", "DROPPED_SUBMIN",
])
def test_a_signal_the_pipeline_decided_against_cannot_be_approved(
    ledger, approve, status
):
    db = ledger
    sid = _signal(db, status=status)

    msg, ok, url = approve("approve", sid)

    assert ok is False, f"{status} must not be approvable"
    assert url is None
    assert status in msg

    with db.get_db() as conn:
        after = conn.execute(
            "SELECT status FROM signals WHERE id=?", (sid,)).fetchone()["status"]
    assert after == status, "a refused approval must leave the signal untouched"
    assert db.get_open_positions() == []


def test_qc_refusal_is_enforced_at_the_approval_boundary(ledger, approve):
    """
    The one that matters most: QC reviewed this trade adversarially and
    returned DISAGREE. Approving it would override that from outside.
    """
    db = ledger
    sid = _signal(db, status="QC_BLOCKED")
    msg, ok, _ = approve("approve", sid)

    assert ok is False
    assert "rejected it" in msg.lower()


@pytest.mark.parametrize("status", ["PENDING", "APPROVED", "NOT_EXECUTED"])
def test_approvable_states_still_work(ledger, approve, status):
    """The guard must not break the paths that are supposed to work."""
    db = ledger
    sid = _signal(db, status=status)
    msg, ok, url = approve("approve", sid)
    assert ok is True
    assert url is not None


def test_reject_is_unaffected_by_the_status_guard(ledger, approve):
    """The guard covers approval only; rejecting stays available."""
    db = ledger
    sid = _signal(db, status="PENDING")
    msg, ok, url = approve("reject", sid)

    assert ok is True
    with db.get_db() as conn:
        assert conn.execute(
            "SELECT status FROM signals WHERE id=?", (sid,)
        ).fetchone()["status"] == "REJECTED"
