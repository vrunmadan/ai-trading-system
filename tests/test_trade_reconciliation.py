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

    def __init__(self, positions=None, holdings=None, fail=False,
                 fail_positions=None, fail_holdings=None):
        self._positions = positions or []
        self._holdings = holdings or []
        # fail=True fails both, for the "Kite is completely unreachable" case.
        # fail_positions/fail_holdings let a test fail just one, to exercise
        # the partial-failure path.
        self.fail_positions = fail if fail_positions is None else fail_positions
        self.fail_holdings = fail if fail_holdings is None else fail_holdings

    def positions(self):
        if self.fail_positions:
            raise RuntimeError("kite down")
        return {"net": self._positions, "day": []}

    def holdings(self):
        if self.fail_holdings:
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

    # PAPER (the default here) never returns a real basket URL — see
    # test_paper_row_is_never_confirmed_from_a_real_kite_holding below and
    # the 2026-09-19 review, item 1.
    assert ok is True and url is None

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

    assert ok is True
    assert url is None          # PAPER: no real basket ever existed to reopen
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
    trade = _approve_live(db, monkeypatch)

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
    trade = _approve_live(db, monkeypatch)

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
    trade = _approve_live(db, monkeypatch)

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
    trade = _approve_live(db, monkeypatch, quantity=100, capital=45_000.0)

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


def test_both_kite_reads_failing_leaves_live_rows_pending(ledger, monkeypatch):
    """
    get_kite_client() can succeed (the token parses fine) while every actual
    request against it fails (the token is expired or revoked). Before the
    fix, holdings()/positions() swallowed their own exceptions and returned
    an empty map indistinguishable from "you hold nothing" — reproduced in
    the 2026-09-19 review as a LIVE row silently written off to NOT_EXECUTED.
    """
    db = ledger
    _approve_live(db, monkeypatch)
    assert len(db.get_open_positions()) == 1

    _use_kite(monkeypatch, FakeKite(fail=True))
    monkeypatch.setattr("monitor.trade_reconciler.MAX_PENDING_AGE_DAYS", 0)

    from monitor.trade_reconciler import reconcile_pending_trades
    summary = reconcile_pending_trades()

    assert summary["skipped"] is True
    assert summary["not_executed"] == 0
    assert len(db.get_open_positions()) == 1
    assert db.get_pending_trades()[0]["fill_status"] == "PENDING"


def test_partial_kite_failure_defers_rather_than_writes_off(ledger, monkeypatch):
    """
    holdings() failing while positions() succeeds (or vice versa) must not be
    read as "not found anywhere" — the failed source might be exactly where
    a settled position now lives. The row must defer, not age out to
    NOT_EXECUTED, no matter how old it is.
    """
    db = ledger
    _approve_live(db, monkeypatch)

    _use_kite(monkeypatch, FakeKite(holdings=[], fail_holdings=True))
    # Old enough that a COMPLETE empty snapshot would have written it off.
    monkeypatch.setattr("monitor.trade_reconciler.MAX_PENDING_AGE_DAYS", 0)

    from monitor.trade_reconciler import reconcile_pending_trades
    summary = reconcile_pending_trades()

    assert summary["skipped"] is False   # positions() DID work, so we ran
    assert summary["not_executed"] == 0
    assert summary["confirmed"] == 0
    assert summary["deferred"] == 1
    assert len(db.get_open_positions()) == 1
    assert db.get_pending_trades()[0]["fill_status"] == "PENDING"


def test_partial_kite_failure_still_confirms_a_real_match(ledger, monkeypatch):
    """A match found in the source that DID work must still confirm —
    incompleteness only matters for a miss, not a hit."""
    db = ledger
    trade = _approve_live(db, monkeypatch)

    _use_kite(monkeypatch, FakeKite(
        positions=[{"tradingsymbol": "ACME", "exchange": "NSE",
                    "quantity": 400, "average_price": 450.0}],
        fail_holdings=True,
    ))

    from monitor.trade_reconciler import reconcile_pending_trades
    summary = reconcile_pending_trades()

    assert summary["confirmed"] == 1
    row = db.get_trade_for_signal(trade["signal_id"])
    assert row["fill_status"] == "CONFIRMED"


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
    """The round trip the whole hybrid exists to enable (LIVE: confirmed
    from a real Kite fill, then closed)."""
    db = ledger
    trade = _approve_live(db, monkeypatch)

    _use_kite(monkeypatch, FakeKite(positions=[
        {"tradingsymbol": "ACME", "exchange": "NSE",
         "quantity": 400, "average_price": 450.0},
    ]))
    from monitor.trade_reconciler import reconcile_pending_trades
    reconcile_pending_trades()

    row = db.get_trade_for_signal(trade["signal_id"])
    db.close_trade(row["id"], exit_price=418.50)     # -7% stop

    assert db.get_open_positions() == []
    assert db.get_all_time_pnl("LIVE") == pytest.approx((418.50 - 450.0) * 400)


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


def test_paper_row_is_never_confirmed_from_a_real_kite_holding(
    ledger, monkeypatch
):
    """
    Regression test for the 2026-09-19 review, item 1: a PAPER row must never
    be confirmed from whatever Kite happens to hold under the same
    ticker/exchange — the user's own pre-existing personal holding, or (before
    the approval-side fix) a real order accidentally placed through a
    paper-mode approval link. A paper approval is a simulation; the real
    market is irrelevant to it, full stop.
    """
    db = ledger
    trade = _approve(db, monkeypatch)                    # PAPER

    _use_kite(monkeypatch, FakeKite(positions=[
        {"tradingsymbol": "ACME", "exchange": "NSE",
         "quantity": 400, "average_price": 461.20},
    ]))

    from monitor.trade_reconciler import reconcile_pending_trades
    summary = reconcile_pending_trades()

    assert summary["simulated"] == 1
    row = db.get_trade_for_signal(trade["signal_id"])
    assert row["entry_price"] == pytest.approx(450.0)      # the simulated/expected price
    assert row["entry_price"] != pytest.approx(461.20)     # NOT the coincidental real fill
    assert "simulated" in (row["fill_note"] or "").lower()


def test_paper_reconciliation_does_not_need_kite(ledger, monkeypatch):
    """
    A Kite outage must not stall paper trading — PAPER rows need nothing
    from the broker, so they must simulate even when Kite is completely
    unreachable (2026-09-19 review, item 1).
    """
    db = ledger
    trade = _approve(db, monkeypatch)                    # PAPER

    import trader.kite_client as kc
    monkeypatch.setattr(
        kc, "get_kite_client",
        lambda: (_ for _ in ()).throw(RuntimeError("kite down")),
    )

    from monitor.trade_reconciler import reconcile_pending_trades
    summary = reconcile_pending_trades()

    assert summary["simulated"] == 1
    assert summary["skipped"] is False
    row = db.get_trade_for_signal(trade["signal_id"])
    assert row["fill_status"] == "CONFIRMED"


def _confirmed_paper_position(db, monkeypatch, entry=450.0, quantity=400):
    trade = _approve(db, monkeypatch, quantity=quantity, capital=entry * quantity)
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
    assert db.get_all_time_pnl("PAPER") == pytest.approx(-16_000.0)


def test_paper_position_is_left_open_while_the_thesis_holds(ledger, monkeypatch):
    db = ledger
    _confirmed_paper_position(db, monkeypatch, entry=450.0)

    _run_monitor(monkeypatch, ltp=470.0)                  # up, nothing triggered

    assert len(db.get_open_positions()) == 1
    assert db.get_all_time_pnl("PAPER") == 0.0


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


# ---------------------------------------------------------------------------
# Baseline-aware fill matching (2026-09-19 review, item 3)
# ---------------------------------------------------------------------------

def _pending_live_trade(db, ticker="ACME", exchange="NSE", quantity=400,
                        capital=180_000.0, baseline_quantity=0):
    """
    A LIVE PENDING row with an explicit baseline_quantity, written directly
    rather than through the approve flow — the approve flow's baseline
    capture calls the real Kite client, which isn't mocked at approval time
    in these tests and always falls back to 0.
    """
    sid = _insert_signal(db, ticker=ticker, exchange=exchange, quantity=quantity,
                         capital=capital)
    db.log_pending_trade(
        signal_id=sid, ticker=ticker, exchange=exchange, direction="BUY",
        quantity=quantity, expected_price=capital / quantity, mode="LIVE",
        baseline_quantity=baseline_quantity,
    )
    return db.get_trade_for_signal(sid)


def test_two_pending_rows_cannot_double_claim_one_holding(ledger, monkeypatch):
    """
    Two LIVE PENDING rows for the same key, but Kite only shows enough new
    shares for one of them. Before the in-run reservation fix, both rows
    would independently match the same Kite quantity and both get marked
    CONFIRMED — inflating recorded exposure beyond what was actually bought.
    """
    db = ledger
    monkeypatch.setenv("KITE_API_KEY", "k")
    _silence_email(monkeypatch)

    t1 = _pending_live_trade(db, quantity=400)
    t2 = _pending_live_trade(db, quantity=400)

    _use_kite(monkeypatch, FakeKite(positions=[
        {"tradingsymbol": "ACME", "exchange": "NSE",
         "quantity": 400, "average_price": 452.0},
    ]))

    from monitor.trade_reconciler import reconcile_pending_trades
    summary = reconcile_pending_trades()

    assert summary["confirmed"] == 1
    assert summary["deferred"] == 1

    row1 = db.get_trade_for_signal(t1["signal_id"])
    row2 = db.get_trade_for_signal(t2["signal_id"])
    statuses = sorted([row1["fill_status"], row2["fill_status"]])
    assert statuses == ["CONFIRMED", "PENDING"]

    confirmed = row1 if row1["fill_status"] == "CONFIRMED" else row2
    assert confirmed["quantity"] == 400  # only the real 400 shares, not 800


def test_baseline_blocks_a_preexisting_holding_from_being_confirmed(ledger, monkeypatch):
    """
    Kite shows exactly the baseline quantity — i.e. nothing new was bought.
    Without subtracting the baseline, this looks identical to a genuine
    fill and gets wrongly confirmed against a holding that predates the order.
    """
    db = ledger
    monkeypatch.setenv("KITE_API_KEY", "k")
    _silence_email(monkeypatch)

    trade = _pending_live_trade(db, quantity=100, baseline_quantity=400)

    _use_kite(monkeypatch, FakeKite(positions=[
        {"tradingsymbol": "ACME", "exchange": "NSE",
         "quantity": 400, "average_price": 452.0},
    ]))

    from monitor.trade_reconciler import reconcile_pending_trades
    summary = reconcile_pending_trades()

    assert summary["confirmed"] == 0
    row = db.get_trade_for_signal(trade["signal_id"])
    assert row["fill_status"] == "PENDING"     # not falsely confirmed from the pre-existing holding


def test_baseline_lets_only_the_incremental_quantity_be_confirmed(ledger, monkeypatch):
    """
    A 400-share pre-existing holding, plus this order's real 50-share fill:
    Kite shows 450 total. Only the 50 above baseline may be claimed.
    """
    db = ledger
    monkeypatch.setenv("KITE_API_KEY", "k")
    _silence_email(monkeypatch)

    trade = _pending_live_trade(db, quantity=50, capital=22_500.0, baseline_quantity=400)

    _use_kite(monkeypatch, FakeKite(positions=[
        {"tradingsymbol": "ACME", "exchange": "NSE",
         "quantity": 450, "average_price": 452.0},
    ]))

    from monitor.trade_reconciler import reconcile_pending_trades
    summary = reconcile_pending_trades()

    assert summary["confirmed"] == 1
    row = db.get_trade_for_signal(trade["signal_id"])
    assert row["fill_status"] == "CONFIRMED"
    assert row["quantity"] == 50               # the increment, not the full 450


def test_oversized_kite_match_is_noted_but_not_all_recorded_as_exposure(ledger, monkeypatch):
    """
    Kite shows far more new quantity than this order requested (e.g. a
    manual top-up placed alongside the approved order). The trade is still
    confirmed for what was asked, with a visible note about the surplus —
    the surplus itself is never silently adopted as this trade's exposure.
    """
    db = ledger
    monkeypatch.setenv("KITE_API_KEY", "k")
    _silence_email(monkeypatch)

    trade = _pending_live_trade(db, quantity=100, capital=45_000.0)

    _use_kite(monkeypatch, FakeKite(positions=[
        {"tradingsymbol": "ACME", "exchange": "NSE",
         "quantity": 500, "average_price": 452.0},
    ]))

    from monitor.trade_reconciler import reconcile_pending_trades
    reconcile_pending_trades()

    row = db.get_trade_for_signal(trade["signal_id"])
    assert row["fill_status"] == "CONFIRMED"
    assert row["quantity"] == 100                    # not 500
    assert "more new share" in row["fill_note"].lower()


# ---------------------------------------------------------------------------
# External-activity detection on already-CONFIRMED LIVE positions
# ---------------------------------------------------------------------------

def _confirmed_live_trade(db, monkeypatch, ticker="ACME", exchange="NSE",
                          quantity=400, fill_price=452.0):
    trade = _pending_live_trade(db, ticker=ticker, exchange=exchange, quantity=quantity)
    _use_kite(monkeypatch, FakeKite(positions=[
        {"tradingsymbol": ticker, "exchange": exchange,
         "quantity": quantity, "average_price": fill_price},
    ]))
    from monitor.trade_reconciler import reconcile_pending_trades
    reconcile_pending_trades()
    return db.get_trade_for_signal(trade["signal_id"])


def test_external_closure_is_flagged_when_kite_no_longer_shows_the_position(ledger, monkeypatch):
    db = ledger
    monkeypatch.setenv("KITE_API_KEY", "k")
    _silence_email(monkeypatch)

    row = _confirmed_live_trade(db, monkeypatch)
    assert row["fill_status"] == "CONFIRMED"

    # Now Kite shows nothing at all for this key — sold outside the system.
    _use_kite(monkeypatch, FakeKite(positions=[], holdings=[]))

    from monitor.trade_reconciler import check_confirmed_live_positions_for_external_activity
    summary = check_confirmed_live_positions_for_external_activity()

    assert summary == {"checked": 1, "flagged": 1, "skipped": False}
    # The ledger row is left untouched — the system never acts on your behalf.
    still_open = db.get_trade_for_signal(row["signal_id"])
    assert still_open["exit_price"] is None
    assert still_open["fill_status"] == "CONFIRMED"


def test_external_closure_is_not_flagged_while_kite_still_shows_the_position(ledger, monkeypatch):
    db = ledger
    monkeypatch.setenv("KITE_API_KEY", "k")
    _silence_email(monkeypatch)

    _confirmed_live_trade(db, monkeypatch)

    # Same FakeKite still in place from the confirming reconcile — still held.
    from monitor.trade_reconciler import check_confirmed_live_positions_for_external_activity
    summary = check_confirmed_live_positions_for_external_activity()

    assert summary == {"checked": 1, "flagged": 0, "skipped": False}


def test_external_closure_check_skips_when_both_kite_reads_fail(ledger, monkeypatch):
    db = ledger
    monkeypatch.setenv("KITE_API_KEY", "k")
    _silence_email(monkeypatch)

    _confirmed_live_trade(db, monkeypatch)

    _use_kite(monkeypatch, FakeKite(fail=True))

    from monitor.trade_reconciler import check_confirmed_live_positions_for_external_activity
    summary = check_confirmed_live_positions_for_external_activity()

    assert summary["skipped"] is True
    assert summary["flagged"] == 0   # never guesses when it can't see the account


def test_external_closure_check_skips_on_an_incomplete_snapshot(ledger, monkeypatch):
    db = ledger
    monkeypatch.setenv("KITE_API_KEY", "k")
    _silence_email(monkeypatch)

    _confirmed_live_trade(db, monkeypatch)

    # holdings() fails, positions() succeeds but (correctly, since this trade
    # is CNC and same-day) no longer includes this position — an incomplete
    # snapshot, not a confirmed absence.
    _use_kite(monkeypatch, FakeKite(positions=[], fail_holdings=True, fail_positions=False))

    from monitor.trade_reconciler import check_confirmed_live_positions_for_external_activity
    summary = check_confirmed_live_positions_for_external_activity()

    assert summary["skipped"] is True
    assert summary["flagged"] == 0
