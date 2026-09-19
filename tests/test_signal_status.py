"""
Tests for the signals.status vocabulary.

Approving used to write status='EXECUTED' directly, asserting a fill nobody
had checked for; REJECTED and NO_RESPONSE both collapsed into 'MISSED', a
value nothing ever read — while the weekly Auditor searched `status` for the
very names that were being discarded.

Now each value means one thing, and EXECUTED is written in exactly one place:
after the reconciler establishes a fill.
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


def _signal(db, ticker="ACME", quantity=400, capital=180_000.0):
    with db.get_db() as conn:
        return conn.execute(
            """
            INSERT INTO signals (
                created_at, ticker, exchange, regime, strategy_bucket, direction,
                technical_score, fundamental_score, confidence_score,
                researcher_rationale, sized_quantity, capital_to_deploy,
                sizer_notes, status
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (db.now_ist(), ticker, "NSE", "bull", "52wk_breakout", "BUY",
             80.0, 70.0, 76.0, "t", quantity, capital, "t", "PENDING"),
        ).lastrowid


def _status(db, signal_id):
    with db.get_db() as conn:
        return conn.execute(
            "SELECT status, user_response FROM signals WHERE id=?", (signal_id,)
        ).fetchone()


class FakeKite:
    def __init__(self, positions=None, holdings=None):
        self._p = positions or []
        self._h = holdings or []

    def positions(self):
        return {"net": self._p, "day": []}

    def holdings(self):
        return self._h


def _approve(db, monkeypatch, sid, mode="PAPER"):
    monkeypatch.setenv("APPROVAL_SECRET", "s")
    monkeypatch.setenv("KITE_API_KEY", "k")
    monkeypatch.setenv("PAPER_MODE", "true" if mode == "PAPER" else "false")
    import alerts.gmail_alert as ga
    monkeypatch.setattr(ga, "send_plain_email", lambda **kw: True)
    return ga.handle_email_action("approve", sid)


# ---------------------------------------------------------------------------
# The response itself
# ---------------------------------------------------------------------------

def test_approve_records_approved_not_executed(ledger, monkeypatch):
    """The core fix: approving is intent, and must not claim a fill."""
    db = ledger
    sid = _signal(db)
    _approve(db, monkeypatch, sid)

    row = _status(db, sid)
    assert row["status"] == "APPROVED"
    assert row["status"] != "EXECUTED"
    assert row["user_response"] == "APPROVED"


def test_reject_records_rejected_not_missed(ledger, monkeypatch):
    db = ledger
    sid = _signal(db)
    import alerts.gmail_alert as ga
    monkeypatch.setattr(ga, "send_plain_email", lambda **kw: True)
    monkeypatch.setenv("APPROVAL_SECRET", "s")
    ga.handle_email_action("reject", sid)

    row = _status(db, sid)
    assert row["status"] == "REJECTED"
    assert row["user_response"] == "REJECTED"


def test_no_response_records_no_response_not_missed(ledger):
    """
    The EOD sweep's outcome must be visible in `status`, because that is the
    column the Auditor's missed-opportunity task reads.
    """
    db = ledger
    sid = _signal(db)
    db.update_signal_response(sid, "NO_RESPONSE")

    assert _status(db, sid)["status"] == "NO_RESPONSE"


def test_no_status_is_ever_the_dead_missed_value(ledger):
    db = ledger
    for response in ("APPROVED", "REJECTED", "NO_RESPONSE"):
        sid = _signal(db)
        db.update_signal_response(sid, response)
        assert _status(db, sid)["status"] != "MISSED"


def test_auditor_vocabulary_now_appears_in_the_status_column(ledger):
    """
    weekly_audit asks Gemini for signals with status NO_RESPONSE / REJECTED /
    SKIPPED / NOT_EXECUTED. Every one of those must be a value the writer
    actually produces, or the missed-opportunity review finds nothing.
    """
    db = ledger
    produced = set()

    for response in ("REJECTED", "NO_RESPONSE"):
        sid = _signal(db)
        db.update_signal_response(sid, response)
        produced.add(_status(db, sid)["status"])

    sid = _signal(db)
    db.skip_signal(sid, "test")
    produced.add(_status(db, sid)["status"])

    sid = _signal(db)
    db.mark_signal_not_executed(sid)
    produced.add(_status(db, sid)["status"])

    assert {"REJECTED", "NO_RESPONSE", "SKIPPED", "NOT_EXECUTED"} <= produced


# ---------------------------------------------------------------------------
# Only a confirmed fill promotes to EXECUTED
# ---------------------------------------------------------------------------

def test_confirmed_fill_promotes_the_signal_to_executed(ledger, monkeypatch):
    db = ledger
    sid = _signal(db)
    _approve(db, monkeypatch, sid, mode="LIVE")
    assert _status(db, sid)["status"] == "APPROVED"

    import trader.kite_client as kc
    monkeypatch.setattr(kc, "get_kite_client", lambda: FakeKite(positions=[
        {"tradingsymbol": "ACME", "exchange": "NSE",
         "quantity": 400, "average_price": 451.0},
    ]))

    from monitor.trade_reconciler import reconcile_pending_trades
    reconcile_pending_trades()

    assert _status(db, sid)["status"] == "EXECUTED"


def test_unfilled_order_marks_the_signal_not_executed(ledger, monkeypatch):
    db = ledger
    sid = _signal(db)
    _approve(db, monkeypatch, sid, mode="LIVE")

    import trader.kite_client as kc
    monkeypatch.setattr(kc, "get_kite_client", lambda: FakeKite())
    monkeypatch.setattr("monitor.trade_reconciler.MAX_PENDING_AGE_DAYS", 0)

    from monitor.trade_reconciler import reconcile_pending_trades
    reconcile_pending_trades()

    assert _status(db, sid)["status"] == "NOT_EXECUTED"


def test_signal_stays_approved_while_the_fill_is_still_unverified(
    ledger, monkeypatch
):
    """Inside the grace window nothing is known yet, so nothing is claimed."""
    db = ledger
    sid = _signal(db)
    _approve(db, monkeypatch, sid, mode="LIVE")

    import trader.kite_client as kc
    monkeypatch.setattr(kc, "get_kite_client", lambda: FakeKite())
    monkeypatch.setattr("monitor.trade_reconciler.MAX_PENDING_AGE_DAYS", 5)

    from monitor.trade_reconciler import reconcile_pending_trades
    reconcile_pending_trades()

    assert _status(db, sid)["status"] == "APPROVED"


def test_kite_outage_does_not_promote_or_demote_anything(ledger, monkeypatch):
    db = ledger
    sid = _signal(db)
    _approve(db, monkeypatch, sid, mode="LIVE")

    import trader.kite_client as kc
    monkeypatch.setattr(
        kc, "get_kite_client",
        lambda: (_ for _ in ()).throw(RuntimeError("no token")),
    )

    from monitor.trade_reconciler import reconcile_pending_trades
    reconcile_pending_trades()

    assert _status(db, sid)["status"] == "APPROVED"


def test_simulated_paper_fill_also_reaches_executed(ledger, monkeypatch):
    """
    Within the simulation the trade did happen, and the Auditor needs it in
    the same bucket as a real fill to calibrate confidence against outcomes.
    """
    db = ledger
    sid = _signal(db)
    _approve(db, monkeypatch, sid, mode="PAPER")

    import trader.kite_client as kc
    monkeypatch.setattr(kc, "get_kite_client", lambda: FakeKite())
    monkeypatch.setattr("monitor.trade_reconciler.MAX_PENDING_AGE_DAYS", 0)

    from monitor.trade_reconciler import reconcile_pending_trades
    reconcile_pending_trades()

    assert _status(db, sid)["status"] == "EXECUTED"


# ---------------------------------------------------------------------------
# Backfill of databases written under the old vocabulary
# ---------------------------------------------------------------------------

def test_backfill_demotes_unverified_executed_and_recovers_missed(monkeypatch):
    """
    The live ledger holds rows written by the old approve path. EXECUTED rows
    with no confirmed trade asserted a fill that never happened; MISSED rows
    lost the REJECTED/NO_RESPONSE distinction that user_response still holds.
    """
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    import importlib

    import ledger.db as db

    monkeypatch.setenv("LEDGER_DB_PATH", path)
    importlib.reload(db)

    schema = os.path.join(os.path.dirname(db.__file__), "schema.sql")
    with sqlite3.connect(path) as conn:
        with open(schema) as f:
            conn.executescript(f.read())
        cols = ("created_at,ticker,regime,strategy_bucket,direction,"
                "technical_score,fundamental_score,confidence_score,"
                "researcher_rationale,sized_quantity,capital_to_deploy,"
                "sizer_notes,status,user_response")
        vals = "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        base = ("2026-08-01", "OLD", "bull", "52wk_breakout", "BUY",
                80.0, 70.0, 80.0, "x", 10, 1000.0, "x")
        conn.execute(f"INSERT INTO signals ({cols}) VALUES {vals}",
                     base + ("EXECUTED", "APPROVED"))     # never verified
        conn.execute(f"INSERT INTO signals ({cols}) VALUES {vals}",
                     base + ("MISSED", "REJECTED"))
        conn.execute(f"INSERT INTO signals ({cols}) VALUES {vals}",
                     base + ("MISSED", "NO_RESPONSE"))
        conn.commit()

    db.init_db()

    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT status, user_response FROM signals ORDER BY id")]

    assert rows[0]["status"] == "APPROVED"       # demoted: no confirmed trade
    assert rows[1]["status"] == "REJECTED"       # recovered from user_response
    assert rows[2]["status"] == "NO_RESPONSE"

    db.init_db()                                  # idempotent
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        again = [dict(r) for r in conn.execute(
            "SELECT status FROM signals ORDER BY id")]
    assert [r["status"] for r in again] == ["APPROVED", "REJECTED", "NO_RESPONSE"]

    try:
        os.unlink(path)
    except OSError:
        pass


def test_backfill_keeps_executed_when_a_confirmed_trade_exists(monkeypatch):
    """A genuinely confirmed fill must survive the demotion pass."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    import importlib

    import ledger.db as db

    monkeypatch.setenv("LEDGER_DB_PATH", path)
    importlib.reload(db)

    schema = os.path.join(os.path.dirname(db.__file__), "schema.sql")
    with sqlite3.connect(path) as conn:
        with open(schema) as f:
            conn.executescript(f.read())
        conn.execute(
            "INSERT INTO signals (created_at,ticker,regime,strategy_bucket,"
            "direction,technical_score,fundamental_score,confidence_score,"
            "researcher_rationale,sized_quantity,capital_to_deploy,sizer_notes,"
            "status,user_response) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("2026-08-01", "REAL", "bull", "52wk_breakout", "BUY",
             80.0, 70.0, 80.0, "x", 10, 1000.0, "x", "EXECUTED", "APPROVED"),
        )
        conn.execute(
            "INSERT INTO trades (signal_id,ticker,direction,quantity,entry_price,"
            "entry_time,mode,exchange,fill_status) VALUES (?,?,?,?,?,?,?,?,?)",
            (1, "REAL", "BUY", 10, 100.0, "2026-08-01 10:00:00", "LIVE",
             "NSE", "CONFIRMED"),
        )
        conn.commit()

    db.init_db()

    with sqlite3.connect(path) as conn:
        status = conn.execute("SELECT status FROM signals WHERE id=1").fetchone()[0]
    assert status == "EXECUTED"

    try:
        os.unlink(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Reject must be guarded the same way approve is (2026-09-19 review, item 8)
# ---------------------------------------------------------------------------

def test_reject_refuses_an_approved_signal_and_leaves_it_unchanged(ledger, monkeypatch):
    """
    Reject had no status guard at all - it unconditionally overwrote
    signals.status to REJECTED no matter what state the signal was already
    in. Approving opens a Kite basket and writes a pending trade record
    (log_pending_trade); rejecting afterward cannot retract either of those,
    so it must be refused rather than silently flipping the signal back.
    """
    db = ledger
    sid = _signal(db)
    _approve(db, monkeypatch, sid, mode="PAPER")
    assert _status(db, sid)["status"] == "APPROVED"

    import alerts.gmail_alert as ga
    monkeypatch.setattr(ga, "send_plain_email", lambda **kw: True)
    message, success, kite_url = ga.handle_email_action("reject", sid)

    assert success is False
    assert kite_url is None
    row = _status(db, sid)
    assert row["status"] == "APPROVED"
    assert row["user_response"] == "APPROVED"


def test_reject_refuses_an_executed_signal_and_leaves_it_unchanged(ledger, monkeypatch):
    db = ledger
    sid = _signal(db)
    _approve(db, monkeypatch, sid, mode="LIVE")

    import trader.kite_client as kc
    monkeypatch.setattr(kc, "get_kite_client", lambda: FakeKite(positions=[
        {"tradingsymbol": "ACME", "exchange": "NSE",
         "quantity": 400, "average_price": 451.0},
    ]))
    from monitor.trade_reconciler import reconcile_pending_trades
    reconcile_pending_trades()
    assert _status(db, sid)["status"] == "EXECUTED"

    import alerts.gmail_alert as ga
    monkeypatch.setattr(ga, "send_plain_email", lambda **kw: True)
    _, success, _ = ga.handle_email_action("reject", sid)

    assert success is False
    assert _status(db, sid)["status"] == "EXECUTED"


def test_reject_refuses_to_repeat_on_an_already_rejected_signal(ledger, monkeypatch):
    """A second tap of the same reject link/email must not re-fire the
    'signal rejected' notification or otherwise be treated as new."""
    db = ledger
    import alerts.gmail_alert as ga
    monkeypatch.setattr(ga, "send_plain_email", lambda **kw: True)
    monkeypatch.setenv("APPROVAL_SECRET", "s")
    sid = _signal(db)

    _, ok1, _ = ga.handle_email_action("reject", sid)
    assert ok1 is True
    assert _status(db, sid)["status"] == "REJECTED"

    _, ok2, _ = ga.handle_email_action("reject", sid)
    assert ok2 is False
    assert _status(db, sid)["status"] == "REJECTED"


def test_reject_still_works_from_pending_and_not_executed(ledger, monkeypatch):
    """The guard must not be so tight it blocks the legitimate cases."""
    db = ledger
    import alerts.gmail_alert as ga
    monkeypatch.setattr(ga, "send_plain_email", lambda **kw: True)
    monkeypatch.setenv("APPROVAL_SECRET", "s")

    sid1 = _signal(db, ticker="AAA")
    _, ok1, _ = ga.handle_email_action("reject", sid1)
    assert ok1 is True
    assert _status(db, sid1)["status"] == "REJECTED"

    sid2 = _signal(db, ticker="BBB")
    db.mark_signal_not_executed(sid2)
    _, ok2, _ = ga.handle_email_action("reject", sid2)
    assert ok2 is True
    assert _status(db, sid2)["status"] == "REJECTED"


# ---------------------------------------------------------------------------
# DB-level uniqueness closes the check-then-insert race (2026-09-19 review, item 8)
# ---------------------------------------------------------------------------

def test_a_second_live_trade_row_for_the_same_signal_is_rejected_by_the_db(ledger):
    """
    The application-level double-approve guard (get_live_trade_for_signal,
    checked in handle_email_action before log_pending_trade) reads and
    writes in two separate statements, so two concurrent approve requests
    could both pass the check and both insert a PENDING row — doubling
    recorded exposure for one order. idx_trades_one_live_per_signal makes
    that impossible at the database level: a second non-NOT_EXECUTED row
    for the same signal_id must fail outright.
    """
    db = ledger
    sid = _signal(db)

    db.log_pending_trade(
        signal_id=sid, ticker="ACME", exchange="NSE", direction="BUY",
        quantity=10, expected_price=100.0, mode="PAPER",
    )

    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        db.log_pending_trade(
            signal_id=sid, ticker="ACME", exchange="NSE", direction="BUY",
            quantity=10, expected_price=100.0, mode="PAPER",
        )


def test_a_not_executed_row_never_blocks_a_genuine_retry(ledger):
    """The index deliberately excludes NOT_EXECUTED rows — a confirmed-dead
    attempt must not stop a legitimate second try at the same signal."""
    db = ledger
    sid = _signal(db)

    trade_id = db.log_pending_trade(
        signal_id=sid, ticker="ACME", exchange="NSE", direction="BUY",
        quantity=10, expected_price=100.0, mode="PAPER",
    )
    db.mark_signal_not_executed(sid)
    with db.get_db() as conn:
        conn.execute(
            "UPDATE trades SET fill_status='NOT_EXECUTED' WHERE id=?", (trade_id,)
        )

    # must not raise
    second_id = db.log_pending_trade(
        signal_id=sid, ticker="ACME", exchange="NSE", direction="BUY",
        quantity=10, expected_price=100.0, mode="PAPER",
    )
    assert second_id != trade_id


def test_init_db_dedupes_existing_duplicate_live_trades_before_adding_the_index(monkeypatch):
    """
    A Railway deployment that ran before this fix could already have two
    live trade rows for one signal_id (the exact bug being closed). init_db()
    must clean that up rather than crash when it adds the unique index:
    the newest row stays live, older duplicates are demoted to NOT_EXECUTED.
    """
    import os
    import sqlite3
    import tempfile

    import ledger.db as db

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setattr(db, "DB_PATH", path)

    # Build a pre-fix database: schema without the new unique index, plus
    # two live (CONFIRMED) trades already sharing one signal_id.
    with sqlite3.connect(path) as conn:
        conn.executescript("""
            CREATE TABLE signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL, ticker TEXT NOT NULL,
                exchange TEXT NOT NULL DEFAULT 'NSE', regime TEXT NOT NULL,
                strategy_bucket TEXT NOT NULL, direction TEXT NOT NULL,
                technical_score REAL, fundamental_score REAL, confidence_score REAL,
                researcher_rationale TEXT, qc_verdict TEXT, qc_rationale TEXT,
                price_at_signal REAL, sized_quantity INTEGER, capital_to_deploy REAL,
                sizer_notes TEXT, alert_sent_at TEXT, user_response TEXT,
                status TEXT NOT NULL DEFAULT 'PENDING'
            );
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id INTEGER REFERENCES signals(id),
                ticker TEXT NOT NULL, direction TEXT NOT NULL, quantity INTEGER NOT NULL,
                entry_price REAL, entry_time TEXT, exit_price REAL, exit_time TEXT,
                pnl REAL, mode TEXT NOT NULL DEFAULT 'PAPER',
                exchange TEXT NOT NULL DEFAULT 'NSE',
                fill_status TEXT NOT NULL DEFAULT 'CONFIRMED', fill_note TEXT
            );
        """)
        conn.execute(
            "INSERT INTO signals (id, created_at, ticker, direction, regime, "
            "strategy_bucket, status) VALUES (1, 'x', 'ACME', 'BUY', 'bull', 'b', 'APPROVED')"
        )
        conn.execute(
            "INSERT INTO trades (id, signal_id, ticker, direction, quantity, "
            "entry_price, mode, exchange, fill_status) "
            "VALUES (1, 1, 'ACME', 'BUY', 10, 100.0, 'PAPER', 'NSE', 'CONFIRMED')"
        )
        conn.execute(
            "INSERT INTO trades (id, signal_id, ticker, direction, quantity, "
            "entry_price, mode, exchange, fill_status) "
            "VALUES (2, 1, 'ACME', 'BUY', 10, 100.0, 'PAPER', 'NSE', 'PENDING')"
        )
        conn.commit()

    # init_db() must not raise, and must leave exactly one live row.
    db.init_db()

    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        rows = {r["id"]: r["fill_status"] for r in conn.execute(
            "SELECT id, fill_status FROM trades ORDER BY id"
        )}
    assert rows[2] != "NOT_EXECUTED", "the newest row must stay live"
    assert rows[1] == "NOT_EXECUTED", "the older duplicate must be demoted"

    try:
        os.unlink(path)
    except OSError:
        pass


def test_a_racing_second_approve_gets_the_friendly_already_approved_message(
    ledger, monkeypatch
):
    """
    Simulates the race idx_trades_one_live_per_signal protects against: two
    approve requests for the same signal both pass the upfront double-
    approve check (get_live_trade_for_signal returns None for both) before
    either has inserted anything. The loser's insert now fails at the
    database with IntegrityError instead of writing a duplicate live trade
    row; handle_email_action must turn that into the same friendly message
    the upfront guard gives, not a raw ledger-failure error.
    """
    db = ledger
    sid = _signal(db)

    import alerts.gmail_alert as ga
    monkeypatch.setattr(ga, "send_plain_email", lambda **kw: True)
    monkeypatch.setenv("APPROVAL_SECRET", "s")
    monkeypatch.setenv("PAPER_MODE", "true")

    real_log_pending_trade = db.log_pending_trade

    def _racing_log_pending_trade(*args, **kwargs):
        # Simulate a concurrent request winning the race: it inserts its
        # own live trade row for this signal first...
        real_log_pending_trade(*args, **kwargs)
        # ...so THIS caller's own insert now violates the unique index,
        # exactly as sqlite would raise it for real.
        import sqlite3
        raise sqlite3.IntegrityError("UNIQUE constraint failed: trades.signal_id")

    monkeypatch.setattr(db, "log_pending_trade", _racing_log_pending_trade)

    message, success, kite_url = ga.handle_email_action("approve", sid)

    assert success is True
    assert "already approved" in message.lower()
    # exactly one live trade row exists — the racing insert never landed.
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM trades WHERE signal_id=?", (sid,)
        ).fetchone()
    assert row["n"] == 1
