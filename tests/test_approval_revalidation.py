"""
Tests for LIVE-approval executable-size revalidation (2026-09-19 review,
item 5).

sized_quantity/capital_to_deploy are computed against the portfolio as it
stood when a signal was GENERATED. Approval links do not expire (item 8),
so by the time a user taps Approve, other signals may have been approved in
between (deploying capital this one was never checked against), the same
ticker may already be held, or the market price may have moved materially.
A PAPER approval risks nothing real and keeps the generation-time number,
but a LIVE handoff must be revalidated against the book and the price as
they are RIGHT NOW, not as they were when the alert was written.
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


@pytest.fixture(autouse=True)
def _pin_capital_constants(monkeypatch):
    """Pin the constants the revalidation reads from risk_sizer.sizer so
    these tests don't depend on the environment."""
    import risk_sizer.sizer as sizer

    monkeypatch.setattr(sizer, "TOTAL_CAPITAL", 1_000_000.0)
    monkeypatch.setattr(sizer, "MIN_POSITION_INR", 10_000.0)


def _insert_signal(db, ticker="ACME", exchange="NSE", quantity=400,
                   capital=180_000.0, status="PENDING"):
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
                80.0, 70.0, 76.0, "test", quantity, capital, "test", status,
            ),
        )
        return cur.lastrowid


def _silence_email(monkeypatch):
    import alerts.gmail_alert as ga

    monkeypatch.setattr(ga, "send_plain_email", lambda **kw: True)


def _live(monkeypatch):
    monkeypatch.setenv("APPROVAL_SECRET", "s")
    monkeypatch.setenv("KITE_API_KEY", "k")
    monkeypatch.setenv("PAPER_MODE", "false")


class FakeKite:
    """
    What get_ltp() calls (.ltp), plus what the item-7 microstructure
    preflight calls (.quote, .instruments, .historical_data) whenever the
    price refresh succeeds — healthy enough (wide circuit buffer, ample
    liquidity) to never trip that check on its own, so these fakes model
    "Kite reachable, price fetched, genuinely tradeable stock" without every
    test needing to reason about microstructure. A fake missing
    instruments()/historical_data() now fails the liquidity check CLOSED
    (2026-09-22 — see trader/kite_client.py), not open, so this must model
    real turnover, not just the quote.
    """

    def __init__(self, price, turnover_per_day=10_00_00_000):
        self.price = price
        self.turnover_per_day = turnover_per_day

    def ltp(self, key):
        return {key: {"last_price": self.price}}

    def quote(self, key):
        return {
            key: {
                "last_price": self.price,
                "upper_circuit_limit": self.price * 1.10,
                "lower_circuit_limit": self.price * 0.90,
            }
        }

    def instruments(self, exchange):
        return [{"tradingsymbol": "ACME", "instrument_token": 1}]

    def historical_data(self, token, from_date, to_date, interval, **kwargs):
        volume = int(self.turnover_per_day / self.price)
        return [{"volume": volume, "close": self.price}] * 20


def _use_kite(monkeypatch, fake):
    import trader.kite_client as kc

    monkeypatch.setattr(kc, "get_kite_client", lambda: fake)


# ---------------------------------------------------------------------------
# No pyramiding, against the CURRENT book
# ---------------------------------------------------------------------------

def test_second_signal_for_an_already_open_live_ticker_is_refused(ledger, monkeypatch):
    """
    Two signals for the same ticker, both approvable at generation time
    (neither held anything yet), but the first approval changes the book
    before the second is approved. The old code only ever checked pyramiding
    against the book AT GENERATION TIME (in the sizer); it never re-checked
    at approval.
    """
    db = ledger
    _live(monkeypatch)
    _silence_email(monkeypatch)
    import alerts.gmail_alert as ga

    sid1 = _insert_signal(db, ticker="ACME")
    _, ok1, _ = ga.handle_email_action("approve", sid1)
    assert ok1 is True

    sid2 = _insert_signal(db, ticker="ACME")
    msg2, ok2, url2 = ga.handle_email_action("approve", sid2)

    assert ok2 is False
    assert url2 is None
    assert "already an open" in msg2.lower()
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT user_response FROM signals WHERE id=?", (sid2,)
        ).fetchone()
    assert row["user_response"] is None       # left untouched, not silently approved
    assert len(db.get_open_positions(mode="LIVE")) == 1


def test_a_different_ticker_is_unaffected_by_the_pyramiding_check(ledger, monkeypatch):
    db = ledger
    _live(monkeypatch)
    _silence_email(monkeypatch)
    import alerts.gmail_alert as ga

    sid1 = _insert_signal(db, ticker="ACME", capital=100_000.0, quantity=200)
    ga.handle_email_action("approve", sid1)

    sid2 = _insert_signal(db, ticker="OTHERCO", capital=100_000.0, quantity=200)
    _, ok2, _ = ga.handle_email_action("approve", sid2)

    assert ok2 is True
    assert len(db.get_open_positions(mode="LIVE")) == 2


# ---------------------------------------------------------------------------
# Free capital, against the CURRENT book
# ---------------------------------------------------------------------------

def test_second_signal_beyond_current_free_capital_is_refused(ledger, monkeypatch):
    """
    Neither signal individually looks like a problem in isolation, but the
    first approval deploys capital the second was never checked against —
    a stale generation-time check never sees this.
    """
    db = ledger
    _live(monkeypatch)
    _silence_email(monkeypatch)
    import risk_sizer.sizer as sizer

    # A smaller capital base makes the exhaustion reproducible with a single
    # prior approval rather than several compounding the 5% safety margin.
    monkeypatch.setattr(sizer, "TOTAL_CAPITAL", 100_000.0)
    import alerts.gmail_alert as ga

    # Deploys 91,000 of the pinned 100,000 -- only 9,000 left free, below
    # the pinned 10,000 minimum.
    sid1 = _insert_signal(db, ticker="ACME", quantity=200, capital=91_000.0)
    _, ok1, _ = ga.handle_email_action("approve", sid1)
    assert ok1 is True

    sid2 = _insert_signal(db, ticker="OTHERCO", quantity=10, capital=9_000.0)
    msg2, ok2, url2 = ga.handle_email_action("approve", sid2)

    assert ok2 is False
    assert url2 is None
    assert "free capital" in msg2.lower()
    live_positions = db.get_open_positions(mode="LIVE")
    assert len(live_positions) == 1
    assert live_positions[0]["ticker"] == "ACME"


def test_a_request_beyond_free_capital_is_shrunk_rather_than_rejected_outright(
    ledger, monkeypatch
):
    """
    When there IS still a viable amount of free capital, but less than this
    signal's generation-time capital_to_deploy, the order is shrunk to fit —
    mirroring how a stale price shrinks quantity — rather than refused
    outright.
    """
    db = ledger
    _live(monkeypatch)
    _silence_email(monkeypatch)
    import risk_sizer.sizer as sizer

    monkeypatch.setattr(sizer, "TOTAL_CAPITAL", 100_000.0)
    import alerts.gmail_alert as ga

    sid1 = _insert_signal(db, ticker="ACME", quantity=200, capital=60_000.0)
    ga.handle_email_action("approve", sid1)   # deploys 60,000; 40,000 free

    # Wants 30,000, but only 40,000*0.95=38,000 max is ever handed out, and
    # this request is within that -- so it should NOT be shrunk here. Use a
    # request that DOES exceed the 38,000 ceiling to see the shrink.
    sid2 = _insert_signal(db, ticker="OTHERCO", quantity=100, capital=39_000.0)
    _, ok2, _ = ga.handle_email_action("approve", sid2)

    assert ok2 is True
    trade2 = [t for t in db.get_open_positions(mode="LIVE") if t["ticker"] == "OTHERCO"][0]
    # capital_to_deploy shrunk to 40,000 * 0.95 = 38,000; price fell back to
    # the generation-time price (390.0 = 39,000 / 100), so quantity shrinks
    # to 38,000 // 390 = 97, not the originally requested 100.
    assert trade2["quantity"] == 97
    assert trade2["quantity"] < 100


# ---------------------------------------------------------------------------
# Price refresh — shrink, never grow, and fail open when Kite is unreachable
# ---------------------------------------------------------------------------

def test_price_refresh_shrinks_quantity_to_whats_actually_affordable(ledger, monkeypatch):
    """
    Generated when ACME was ~₹450 (180,000 / 400). By approval time the
    price has risen to ₹600 — 400 shares would now cost ₹240,000, more than
    was ever approved. The order must shrink to what ₹180,000 actually buys
    at the CURRENT price.
    """
    db = ledger
    _live(monkeypatch)
    _silence_email(monkeypatch)
    import alerts.gmail_alert as ga

    sid = _insert_signal(db, ticker="ACME", quantity=400, capital=180_000.0)
    _use_kite(monkeypatch, FakeKite(price=600.0))

    _, ok, _ = ga.handle_email_action("approve", sid)

    assert ok is True
    trade = db.get_pending_trades()[0]
    assert trade["quantity"] == 300                       # 180,000 // 600
    assert trade["entry_price"] == pytest.approx(600.0)


def test_price_refresh_never_grows_quantity_past_what_was_sized(ledger, monkeypatch):
    """
    A price DROP is not license to buy more than was ever approved — the
    sizer's decision at generation time is still the ceiling, not just a
    floor.
    """
    db = ledger
    _live(monkeypatch)
    _silence_email(monkeypatch)
    import alerts.gmail_alert as ga

    sid = _insert_signal(db, ticker="ACME", quantity=400, capital=180_000.0)
    # ₹180,000 would now buy 600 shares at this price -- must stay at 400.
    _use_kite(monkeypatch, FakeKite(price=300.0))

    _, ok, _ = ga.handle_email_action("approve", sid)

    assert ok is True
    trade = db.get_pending_trades()[0]
    assert trade["quantity"] == 400
    assert trade["entry_price"] == pytest.approx(300.0)


def test_price_refresh_failure_falls_back_to_the_generation_time_price(ledger, monkeypatch):
    """
    Kite being unreachable at approval time must not block the order —
    fall back to the number computed when the signal was generated, exactly
    as before this fix (never worse than before it).
    """
    db = ledger
    _live(monkeypatch)
    _silence_email(monkeypatch)
    import alerts.gmail_alert as ga

    sid = _insert_signal(db, ticker="ACME", quantity=400, capital=180_000.0)
    # No _use_kite call: get_kite_client() fails for lack of a token.

    _, ok, _ = ga.handle_email_action("approve", sid)

    assert ok is True
    trade = db.get_pending_trades()[0]
    assert trade["quantity"] == 400
    assert trade["entry_price"] == pytest.approx(450.0)


def test_quantity_shrunk_to_zero_is_refused_rather_than_a_zero_share_order(ledger, monkeypatch):
    db = ledger
    _live(monkeypatch)
    _silence_email(monkeypatch)
    import alerts.gmail_alert as ga

    sid = _insert_signal(db, ticker="ACME", quantity=400, capital=180_000.0)
    # Price spiked so far that even the full approved capital buys 0 shares.
    _use_kite(monkeypatch, FakeKite(price=1_000_000.0))

    msg, ok, url = ga.handle_email_action("approve", sid)

    assert ok is False
    assert url is None
    assert "0 shares" in msg.lower() or "does not buy" in msg.lower()
    assert db.get_pending_trades() == []


def test_paper_approval_is_never_revalidated(ledger, monkeypatch):
    """
    PAPER risks nothing real, so it must keep using the generation-time
    number untouched — no pyramiding check, no free-capital check, no price
    refresh. (PAPER_MODE defaults to true; _live() is deliberately not
    called here.)
    """
    db = ledger
    monkeypatch.setenv("APPROVAL_SECRET", "s")
    _silence_email(monkeypatch)
    import alerts.gmail_alert as ga

    sid1 = _insert_signal(db, ticker="ACME", quantity=400, capital=180_000.0)
    ga.handle_email_action("approve", sid1)

    # A second PAPER signal for the SAME ticker must not be blocked by the
    # LIVE-only pyramiding check.
    sid2 = _insert_signal(db, ticker="ACME", quantity=100, capital=45_000.0)
    _, ok2, _ = ga.handle_email_action("approve", sid2)

    assert ok2 is True
    trade2 = [t for t in db.get_open_positions() if t["signal_id"] == sid2][0]
    assert trade2["quantity"] == 100
    assert trade2["entry_price"] == pytest.approx(450.0)
