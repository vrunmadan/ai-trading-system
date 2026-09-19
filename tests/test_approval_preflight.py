"""
Tests for the LIVE-approval liquidity/circuit preflight gate (2026-09-19
review, item 7).

trader.kite_client.microstructure_checks() (circuit buffer, 20-day turnover,
ADV%) already existed, but the only caller was execute_trade() — dormant,
unreachable from the real approval flow (alerts.gmail_alert builds a Kite
basket URL directly and never calls it). Approval never ran these checks at
all. This wires microstructure_checks() itself (never execute_trade, which
has its own known bug — see its docstring) into the LIVE approval path,
right before the Kite basket is generated.
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
def _reset_instrument_cache():
    """microstructure_checks()'s liquidity lookup uses a module-level
    instrument-token cache shared across the whole test session — reset it
    so a real fetch is attempted (and gracefully degrades to 0.0 turnover
    when it can't be found) rather than reusing another test file's cache."""
    import trader.kite_client as kc

    kc._instrument_token_cache = {}
    yield
    kc._instrument_token_cache = {}


def _insert_signal(db, ticker="ACME", exchange="NSE", quantity=400,
                   capital=180_000.0):
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


def _silence_email(monkeypatch):
    import alerts.gmail_alert as ga

    monkeypatch.setattr(ga, "send_plain_email", lambda **kw: True)


def _live(monkeypatch):
    monkeypatch.setenv("APPROVAL_SECRET", "s")
    monkeypatch.setenv("KITE_API_KEY", "k")
    monkeypatch.setenv("PAPER_MODE", "false")


class NearCircuitKite:
    """Price refreshes fine, but sits right at the upper circuit."""

    def __init__(self, price=450.0):
        self.price = price

    def ltp(self, key):
        return {key: {"last_price": self.price}}

    def quote(self, key):
        return {
            key: {
                "last_price": self.price,
                "upper_circuit_limit": self.price * 1.002,   # 0.2% away
                "lower_circuit_limit": self.price * 0.80,
            }
        }


class HealthyKite:
    def __init__(self, price=450.0):
        self.price = price

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


def _use_kite(monkeypatch, fake):
    import trader.kite_client as kc

    monkeypatch.setattr(kc, "get_kite_client", lambda: fake)


def test_approval_is_refused_when_near_the_circuit_limit(ledger, monkeypatch):
    db = ledger
    _live(monkeypatch)
    _silence_email(monkeypatch)
    import alerts.gmail_alert as ga

    sid = _insert_signal(db, ticker="ACME")
    _use_kite(monkeypatch, NearCircuitKite(price=450.0))

    msg, ok, url = ga.handle_email_action("approve", sid)

    assert ok is False
    assert url is None
    assert "preflight" in msg.lower()
    assert "circuit" in msg.lower()
    assert db.get_pending_trades() == []


def test_approval_proceeds_for_a_healthy_quote(ledger, monkeypatch):
    """Sanity check: the new gate must not turn every ordinary LIVE approval
    into a refusal."""
    db = ledger
    _live(monkeypatch)
    _silence_email(monkeypatch)
    import alerts.gmail_alert as ga

    sid = _insert_signal(db, ticker="ACME")
    _use_kite(monkeypatch, HealthyKite(price=450.0))

    _, ok, url = ga.handle_email_action("approve", sid)

    assert ok is True
    assert url is not None
    assert len(db.get_pending_trades()) == 1


def test_preflight_is_skipped_rather_than_blocking_when_kite_is_unreachable(
    ledger, monkeypatch
):
    """
    If Kite is down, the price refresh already falls back to the
    generation-time number rather than blocking approval (item 5's
    fail-open design). The preflight check must not turn that same
    unreachability into a NEW, different-looking block — it only runs (and
    can only refuse) when Kite was actually reachable this cycle.
    """
    db = ledger
    _live(monkeypatch)
    _silence_email(monkeypatch)
    import alerts.gmail_alert as ga

    sid = _insert_signal(db, ticker="ACME", quantity=400, capital=180_000.0)
    # No _use_kite call: get_kite_client() fails for lack of a token, exactly
    # as in the item-5 fallback tests.

    _, ok, url = ga.handle_email_action("approve", sid)

    assert ok is True
    assert url is not None
    trade = db.get_pending_trades()[0]
    assert trade["quantity"] == 400
    assert trade["entry_price"] == pytest.approx(450.0)


def test_preflight_is_never_run_for_paper_approvals(ledger, monkeypatch):
    """PAPER never touches Kite at all for this — including for a preflight
    check that would otherwise apply only to a real handoff."""
    db = ledger
    monkeypatch.setenv("APPROVAL_SECRET", "s")
    _silence_email(monkeypatch)
    import alerts.gmail_alert as ga

    # A circuit-tight FakeKite that WOULD fail the preflight if it ran.
    _use_kite(monkeypatch, NearCircuitKite(price=450.0))

    sid = _insert_signal(db, ticker="ACME", quantity=400, capital=180_000.0)
    _, ok, url = ga.handle_email_action("approve", sid)

    assert ok is True
    assert url is None      # PAPER never gets a basket URL either way
    assert len(db.get_pending_trades()) == 1
