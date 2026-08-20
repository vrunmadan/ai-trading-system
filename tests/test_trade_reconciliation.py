"""
Tests for the approve -> PENDING -> reconcile hybrid.

Approving a signal records intent as a PENDING trade row so same-day exposure
is visible to the portfolio gate immediately. The EOD reconciler then asks Kite
what actually filled and promotes each row to CONFIRMED (with the real average
price) or NOT_EXECUTED (which stops counting toward exposure).
"""

import os
import sqlite3
import tempfile

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def ledger(monkeypatch):
    """A real SQLite ledger built from schema.sql + the idempotent migrations."""
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


def _insert_signal(db, quantity=400, capital=180_000.0, exchange="NSE",
                   ticker="ACME"):
    with db.get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO signals (
                created_at, ticker, exchange, regime, strategy_bucket, direction,
                technical_score, fundamental_score, confidence_score,
                researcher_rationale, sized_quantity, capital_to_deploy,
                sizer_notes, status
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                db.now_ist(), ticker, exchange, "bull", "52wk_breakout", "BUY",
                80.0, 70.0, 76.0, "test", quantity, capital, "test", "PENDING",
            ),
        )
        return cur.lastrowid


class FakeKite:
    """Minimal stand-in for KiteConnect: only what the reconciler calls."""

    def __init__(self, positions=None, holdings=None, fail=False):
        self._positions = positions or []
        self._holdings = holdings or []
        self.fail = fail

    def positions(self):
        if self.fail:
            raise RuntimeError("kite down")
        return {"net": self._positions, "day": []}

    def holdings(self):
        if self.fail:
            raise RuntimeError("kite down")
        return self._holdings


def _use_kite(monkeypatch, fake):
    import trader.kite_client as kc

    monkeypatch.setattr(kc, "get_kite_client", lambda: fake)


def _silence_email(monkeypatch):
    import alerts.gmail_alert as ga

    monkeypatch.setattr(ga, "send_plain_email", lambda **kw: True)


# ---------------------------------------------------------------------------
# Approve writes a PENDING row
# ---------------------------------------------------------------------------

def test_approve_writes_pending_trade_with_real_quantity(ledger, monkeypatch):
    db = ledger
    monkeypatch.setenv("APPROVAL_SECRET", "s")
    monkeypatch.setenv("KITE_API_KEY", "k")
    _silence_email(monkeypatch)

    import alerts.gmail_alert as ga

    sid = _insert_signal(db, quantity=400, capital=180_000.0)
    _, ok, url = ga.handle_email_action("approve", sid)

    assert ok is True and url is not None

    trades = db.get_open_positions()
    assert len(trades) == 1
    t = trades[0]
    assert t["quantity"] == 400
    assert t["fill_status"] == "PENDING"
    assert t["exchange"] == "NSE"
    assert t["signal_id"] == sid
    # entry_price is the expected price until the reconciler learns the truth
    assert t["entry_price"] == pytest.approx(450.0)


def test_pending_trade_counts_toward_exposure_immediately(ledger, monkeypatch):
    """
    The whole point of writing at approve time: a second signal approved the
    same day must see the first one's capital.
    """
    db = ledger
    monkeypatch.setenv("APPROVAL_SECRET", "s")
    monkeypatch.setenv("KITE_API_KEY", "k")
    _silence_email(monkeypatch)

    import alerts.gmail_alert as ga

    ga.handle_email_action("approve", _insert_signal(db, ticker="AAA"))
    ga.handle_email_action("approve", _insert_signal(db, ticker="BBB"))

    open_now = db.get_open_positions()
    assert len(open_now) == 2
    deployed = sum(p["entry_price"] * p["quantity"] for p in open_now)
    assert deployed == pytest.approx(360_000.0)


def test_second_approve_does_not_write_a_second_row(ledger, monkeypatch):
    """Approval links never expire; a double tap must not double the exposure."""
    db = ledger
    monkeypatch.setenv("APPROVAL_SECRET", "s")
    monkeypatch.setenv("KITE_API_KEY", "k")
    _silence_email(monkeypatch)

    import alerts.gmail_alert as ga

    sid = _insert_signal(db)
    ga.handle_email_action("approve", sid)
    msg, ok, url = ga.handle_email_action("approve", sid)

    assert ok is True          # still redirects, so the user is not stranded
    assert url is not None
    assert "already approved" in msg.lower()
    assert len(db.get_open_positions()) == 1


def test_approve_writes_no_trade_when_quantity_is_missing(ledger, monkeypatch):
    db = ledger
    monkeypatch.setenv("APPROVAL_SECRET", "s")
    _silence_email(monkeypatch)

    import alerts.gmail_alert as ga

    sid = _insert_signal(db, quantity=0)
    _, ok, url = ga.handle_email_action("approve", sid)

    assert ok is False and url is None
    assert db.get_open_positions() == []


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------

def _approve(db, monkeypatch, mode="PAPER", **kw):
    monkeypatch.setenv("APPROVAL_SECRET", "s")
    monkeypatch.setenv("KITE_API_KEY", "k")
    monkeypatch.setenv("PAPER_MODE", "true" if mode == "PAPER" else "false")
    _silence_email(monkeypatch)
    import alerts.gmail_alert as ga
    sid = _insert_signal(db, **kw)
    ga.handle_email_action("approve", sid)
    return db.get_pending_trades()[0]


def _approve_live(db, monkeypatch, **kw):
    """A real order. Only these can be written off as NOT_EXECUTED."""
    return _approve(db, monkeypatch, mode="LIVE", **kw)


def test_reconcile_confirms_from_positions_with_real_average_price(ledger, monkeypatch):
    db = ledger
    trade = _approve(db, monkeypatch)

    _use_kite(monkeypatch, FakeKite(positions=[
        {"tradingsymbol": "ACME", "exchange": "NSE",
         "quantity": 400, "average_price": 452.75},
    ]))

    from monitor.trade_reconciler import reconcile_pending_trades
    summary = reconcile_pending_trades()

    assert summary["confirmed"] == 1
    assert summary["not_executed"] == 0

    row = db.get_trade_for_signal(trade["signal_id"])
    assert row["fill_status"] == "CONFIRMED"
    assert row["entry_price"] == pytest.approx(452.75)   # real fill, not the estimate
    assert row["quantity"] == 400


def test_reconcile_confirms_from_holdings_when_not_in_positions(ledger, monkeypatch):
    db = ledger
    trade = _approve(db, monkeypatch)

    _use_kite(monkeypatch, FakeKite(holdings=[
        {"tradingsymbol": "ACME", "exchange": "NSE",
         "quantity": 400, "average_price": 449.10},
    ]))

    from monitor.trade_reconciler import reconcile_pending_trades
    assert reconcile_pending_trades()["confirmed"] == 1

    row = db.get_trade_for_signal(trade["signal_id"])
    assert row["fill_status"] == "CONFIRMED"
    assert row["entry_price"] == pytest.approx(449.10)


def test_reconcile_records_a_partial_fill(ledger, monkeypatch):
    db = ledger
    trade = _approve(db, monkeypatch)

    _use_kite(monkeypatch, FakeKite(positions=[
        {"tradingsymbol": "ACME", "exchange": "NSE",
         "quantity": 150, "average_price": 451.0},
    ]))

    from monitor.trade_reconciler import reconcile_pending_trades
    assert reconcile_pending_trades()["confirmed"] == 1

    row = db.get_trade_for_signal(trade["signal_id"])
    assert row["fill_status"] == "CONFIRMED"
    assert row["quantity"] == 150
    assert "partial" in (row["fill_note"] or "").lower()


def test_reconcile_never_claims_more_than_it_asked_for(ledger, monkeypatch):
    """A larger Kite holding means the user already owned some. Not ours."""
    db = ledger
    trade = _approve(db, monkeypatch, quantity=100, capital=45_000.0)

    _use_kite(monkeypatch, FakeKite(holdings=[
        {"tradingsymbol": "ACME", "exchange": "NSE",
         "quantity": 5000, "average_price": 450.0},
    ]))

    from monitor.trade_reconciler import reconcile_pending_trades
    reconcile_pending_trades()

    assert db.get_trade_for_signal(trade["signal_id"])["quantity"] == 100


def test_reconcile_matches_on_exchange_not_just_ticker(ledger, monkeypatch):
    """A BSE approval must not be confirmed by an NSE holding of the same name."""
    db = ledger
    trade = _approve_live(db, monkeypatch, exchange="BSE")

    _use_kite(monkeypatch, FakeKite(holdings=[
        {"tradingsymbol": "ACME", "exchange": "NSE",
         "quantity": 400, "average_price": 450.0},
    ]))
    monkeypatch.setattr(
        "monitor.trade_reconciler.MAX_PENDING_AGE_DAYS", 0
    )

    from monitor.trade_reconciler import reconcile_pending_trades
    summary = reconcile_pending_trades()

    assert summary["confirmed"] == 0
    assert summary["not_executed"] == 1
    assert db.get_trade_for_signal(trade["signal_id"])["fill_status"] == "NOT_EXECUTED"


def test_unfilled_order_is_marked_not_executed_and_drops_out_of_exposure(
    ledger, monkeypatch
):
    """A LIVE order that never reached the market stops counting as exposure."""
    db = ledger
    _approve_live(db, monkeypatch)
    assert len(db.get_open_positions()) == 1

    _use_kite(monkeypatch, FakeKite())          # Kite holds nothing
    monkeypatch.setattr("monitor.trade_reconciler.MAX_PENDING_AGE_DAYS", 0)

    from monitor.trade_reconciler import reconcile_pending_trades
    assert reconcile_pending_trades()["not_executed"] == 1

    # The row survives for the audit trail but stops counting as exposure.
    assert db.get_open_positions() == []
    assert db.get_trade_for_signal(1)["fill_status"] == "NOT_EXECUTED"


def test_missing_fill_is_deferred_inside_the_grace_period(ledger, monkeypatch):
    """
    Settlement lag or a skipped run must not erase a real position. Within the
    grace window the row stays PENDING for the next attempt.
    """
    db = ledger
    _approve_live(db, monkeypatch)

    _use_kite(monkeypatch, FakeKite())
    monkeypatch.setattr("monitor.trade_reconciler.MAX_PENDING_AGE_DAYS", 2)

    from monitor.trade_reconciler import reconcile_pending_trades
    summary = reconcile_pending_trades()

    assert summary["deferred"] == 1
    assert summary["not_executed"] == 0
    assert db.get_pending_trades()[0]["fill_status"] == "PENDING"


def test_kite_unreachable_leaves_rows_pending_rather_than_wiping_them(
    ledger, monkeypatch
):
    """
    Fail closed. Marking rows NOT_EXECUTED because Kite was down would silently
    erase real exposure from the risk gates.
    """
    db = ledger
    _approve_live(db, monkeypatch)

    import trader.kite_client as kc
    monkeypatch.setattr(
        kc, "get_kite_client",
        lambda: (_ for _ in ()).throw(RuntimeError("no token")),
    )

    from monitor.trade_reconciler import reconcile_pending_trades
    summary = reconcile_pending_trades()

    assert summary["skipped"] is True
    assert summary["not_executed"] == 0
    assert len(db.get_open_positions()) == 1


def test_reconcile_is_a_noop_with_nothing_pending(ledger, monkeypatch):
    """No PENDING rows must mean no Kite call at all."""
    def boom():
        raise AssertionError("Kite must not be contacted when nothing is pending")

    import trader.kite_client as kc
    monkeypatch.setattr(kc, "get_kite_client", boom)

    from monitor.trade_reconciler import reconcile_pending_trades
    assert reconcile_pending_trades() == {
        "pending": 0, "confirmed": 0, "not_executed": 0,
        "deferred": 0, "simulated": 0, "skipped": False,
    }


def test_confirmed_trade_closes_and_feeds_pnl_back_to_the_risk_gate(
    ledger, monkeypatch
):
    """The round trip the whole hybrid exists to enable."""
    db = ledger
    trade = _approve(db, monkeypatch)

    _use_kite(monkeypatch, FakeKite(positions=[
        {"tradingsymbol": "ACME", "exchange": "NSE",
         "quantity": 400, "average_price": 450.0},
    ]))
    from monitor.trade_reconciler import reconcile_pending_trades
    reconcile_pending_trades()

    row = db.get_trade_for_signal(trade["signal_id"])
    db.close_trade(row["id"], exit_price=418.50)     # -7% stop

    assert db.get_open_positions() == []
    assert db.get_all_time_pnl() == pytest.approx((418.50 - 450.0) * 400)


# ---------------------------------------------------------------------------
# PAPER mode: simulated fills, and the round trip closing itself
# ---------------------------------------------------------------------------

def test_paper_trade_is_confirmed_as_a_simulated_fill(ledger, monkeypatch):
    """
    Nothing is ever sent to Kite in PAPER mode, so looking for it there and
    writing it off would make paper trading impossible. It confirms instead.
    """
    db = ledger
    trade = _approve(db, monkeypatch)                    # PAPER

    _use_kite(monkeypatch, FakeKite())                   # Kite holds nothing
    monkeypatch.setattr("monitor.trade_reconciler.MAX_PENDING_AGE_DAYS", 0)

    from monitor.trade_reconciler import reconcile_pending_trades
    summary = reconcile_pending_trades()

    assert summary["confirmed"] == 1
    assert summary["simulated"] == 1
    assert summary["not_executed"] == 0

    row = db.get_trade_for_signal(trade["signal_id"])
    assert row["fill_status"] == "CONFIRMED"
    assert "simulated" in (row["fill_note"] or "").lower()
    assert len(db.get_open_positions()) == 1


def test_paper_trade_found_in_kite_uses_the_real_fill_not_the_simulation(
    ledger, monkeypatch
):
    """
    PAPER_MODE does not currently gate the approval path, so a paper-tagged
    signal can still be placed for real. If Kite has it, the truth wins.
    """
    db = ledger
    trade = _approve(db, monkeypatch)                    # PAPER

    _use_kite(monkeypatch, FakeKite(positions=[
        {"tradingsymbol": "ACME", "exchange": "NSE",
         "quantity": 400, "average_price": 461.20},
    ]))

    from monitor.trade_reconciler import reconcile_pending_trades
    summary = reconcile_pending_trades()

    assert summary["simulated"] == 0
    row = db.get_trade_for_signal(trade["signal_id"])
    assert row["entry_price"] == pytest.approx(461.20)
    assert "simulated" not in (row["fill_note"] or "").lower()


def _confirmed_paper_position(db, monkeypatch, entry=450.0):
    trade = _approve(db, monkeypatch)
    _use_kite(monkeypatch, FakeKite(positions=[
        {"tradingsymbol": "ACME", "exchange": "NSE",
         "quantity": 400, "average_price": entry},
    ]))
    from monitor.trade_reconciler import reconcile_pending_trades
    reconcile_pending_trades()
    return db.get_trade_for_signal(trade["signal_id"])


class MonitorKite:
    def __init__(self, ltp):
        self.ltp_value = ltp

    def ltp(self, key):
        return {key: {"last_price": self.ltp_value}}


def _run_monitor(monkeypatch, ltp, regime="bull"):
    import trader.kite_client as kc
    import monitor.position_monitor as pm

    monkeypatch.setattr(kc, "get_kite_client", lambda: MonitorKite(ltp))
    monkeypatch.setattr(pm, "_send_monitor_email", lambda text: None)

    class Reading:
        class regime_enum:
            value = regime
        regime = regime_enum()

    import researcher.regime_classifier as rc
    monkeypatch.setattr(rc, "classify_regime", lambda **kw: Reading())
    pm.check_open_positions()


def test_paper_position_is_closed_when_the_stop_triggers(ledger, monkeypatch):
    """The round trip this whole chain exists to enable."""
    db = ledger
    row = _confirmed_paper_position(db, monkeypatch, entry=450.0)
    assert len(db.get_open_positions()) == 1

    _run_monitor(monkeypatch, ltp=410.0)                  # -8.9%, below the -7% stop

    closed = db.get_trade_for_signal(row["signal_id"])
    assert closed["exit_price"] == pytest.approx(410.0)
    assert closed["exit_time"] is not None
    assert closed["pnl"] == pytest.approx((410.0 - 450.0) * 400)
    assert db.get_open_positions() == []
    # and the portfolio gate can finally see a real number
    assert db.get_all_time_pnl() == pytest.approx(-16_000.0)


def test_paper_position_is_left_open_while_the_thesis_holds(ledger, monkeypatch):
    db = ledger
    _confirmed_paper_position(db, monkeypatch, entry=450.0)

    _run_monitor(monkeypatch, ltp=470.0)                  # up, nothing triggered

    assert len(db.get_open_positions()) == 1
    assert db.get_all_time_pnl() == 0.0


def test_paper_position_closes_at_the_observed_price_not_the_stop_line(
    ledger, monkeypatch
):
    """
    Filling at the stop line is the optimistic assumption the backtest makes.
    The live monitor only learns the stop broke at 15:35, so the honest
    simulated exit is the price it actually saw.
    """
    db = ledger
    row = _confirmed_paper_position(db, monkeypatch, entry=450.0)
    stop_line = 450.0 * 0.93                              # 418.50

    _run_monitor(monkeypatch, ltp=400.0)                  # gapped well below

    closed = db.get_trade_for_signal(row["signal_id"])
    assert closed["exit_price"] == pytest.approx(400.0)
    assert closed["exit_price"] < stop_line


def test_live_position_is_never_auto_closed(ledger, monkeypatch):
    """
    The system does not act on your behalf. Closing the ledger row while the
    real Kite position is still open would assert an exit that never happened.
    """
    db = ledger
    trade = _approve_live(db, monkeypatch)
    _use_kite(monkeypatch, FakeKite(positions=[
        {"tradingsymbol": "ACME", "exchange": "NSE",
         "quantity": 400, "average_price": 450.0},
    ]))
    from monitor.trade_reconciler import reconcile_pending_trades
    reconcile_pending_trades()

    _run_monitor(monkeypatch, ltp=410.0)                  # stop broken

    row = db.get_trade_for_signal(trade["signal_id"])
    assert row["exit_price"] is None
    assert len(db.get_open_positions()) == 1
