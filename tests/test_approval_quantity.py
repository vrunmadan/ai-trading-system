"""
Regression tests for the quantity bug.

Before this fix:
  - risk_sizer returned quantity=0 on the approved path and nothing ever
    filled it in, so signals.sized_quantity was always 0;
  - alerts/gmail_alert.handle_email_action read it back as
    `row["sized_quantity"] or 1`, and because 0 is falsy in Python every
    approved trade opened a Kite basket for exactly 1 share;
  - the alert email rendered "Quantity: 0 shares" next to a five-figure
    capital number.

These tests pin both halves: the capital -> shares conversion, and the
refusal to invent a quantity when one is missing.
"""

import os
import sqlite3
import tempfile

import pytest


# ---------------------------------------------------------------------------
# The conversion itself (run_cycle Step 3b)
# ---------------------------------------------------------------------------

def _shares(capital_to_deploy: float, ltp: float) -> int:
    """Mirrors the conversion in main.run_cycle Step 3b."""
    return int(capital_to_deploy // ltp)


@pytest.mark.parametrize(
    "capital, ltp, expected",
    [
        (180_000.0, 450.0, 400),    # the review's worked example
        (148_800.0, 1_240.0, 120),
        (10_000.0, 9_999.0, 1),     # exactly one share
        (200_000.0, 33.33, 6000),   # cheap stock, large quantity
        (100_000.0, 100_000.0, 1),  # capital equals one share
    ],
)
def test_capital_converts_to_whole_shares(capital, ltp, expected):
    assert _shares(capital, ltp) == expected


def test_conversion_never_rounds_up_past_the_budget():
    """Flooring matters: rounding up would overspend the sizer's budget."""
    capital, ltp = 10_000.0, 3_000.0
    qty = _shares(capital, ltp)
    assert qty == 3
    assert qty * ltp <= capital


def test_capital_below_one_share_yields_zero_not_one():
    """
    The old `or 1` turned this case into a 1-share order. The signal must be
    dropped instead, because the sizer already decided this position is too
    small to be worth the transaction cost.
    """
    assert _shares(5_000.0, 12_000.0) == 0


# ---------------------------------------------------------------------------
# The approval path must refuse to invent a quantity
# ---------------------------------------------------------------------------

@pytest.fixture()
def ledger(monkeypatch):
    """A real SQLite ledger with the signals table, wired into ledger.db."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    import ledger.db as db

    monkeypatch.setattr(db, "DB_PATH", path)

    schema = os.path.join(os.path.dirname(db.__file__), "schema.sql")
    with sqlite3.connect(path) as conn:
        with open(schema) as f:
            conn.executescript(f.read())
        try:
            conn.execute(
                "ALTER TABLE signals ADD COLUMN exchange TEXT NOT NULL DEFAULT 'NSE'"
            )
        except sqlite3.OperationalError:
            pass
        conn.commit()

    yield db, path

    try:
        os.unlink(path)
    except OSError:
        pass


def _insert_signal(db, sized_quantity):
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
                db.now_ist(), "ACME", "NSE", "bull", "52wk_breakout", "BUY",
                80.0, 70.0, 76.0, "test", sized_quantity, 180_000.0, "test", "PENDING",
            ),
        )
        return cur.lastrowid


@pytest.mark.parametrize("bad_quantity", [0, None])
def test_approve_refuses_when_quantity_is_missing(ledger, bad_quantity, monkeypatch):
    """
    A zero or NULL sized_quantity must NOT become a 1-share basket.
    handle_email_action returns success=False and no basket URL.
    """
    db, _ = ledger
    monkeypatch.setenv("APPROVAL_SECRET", "test-secret")

    import alerts.gmail_alert as ga

    signal_id = _insert_signal(db, bad_quantity)
    message, success, basket_url = ga.handle_email_action("approve", signal_id)

    assert success is False
    assert basket_url is None
    assert "quantity" in message.lower()

    # The signal must be left alone, not marked APPROVED.
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT status, user_response FROM signals WHERE id=?", (signal_id,)
        ).fetchone()
    assert row["status"] == "PENDING"
    assert row["user_response"] is None


def test_approve_uses_the_real_quantity(ledger, monkeypatch):
    """A properly sized signal produces a basket carrying that exact quantity."""
    db, _ = ledger
    monkeypatch.setenv("APPROVAL_SECRET", "test-secret")
    monkeypatch.setenv("KITE_API_KEY", "test-key")

    import alerts.gmail_alert as ga

    sent = {}
    monkeypatch.setattr(
        ga, "send_plain_email", lambda **kw: sent.update(kw) or True
    )

    signal_id = _insert_signal(db, 400)
    message, success, basket_url = ga.handle_email_action("approve", signal_id)

    assert success is True
    assert basket_url is not None
    assert "400" in message
    # The quantity must appear in the basket payload, and 1 must not be
    # standing in for it.
    assert "400" in basket_url

    with db.get_db() as conn:
        row = conn.execute(
            "SELECT status, user_response FROM signals WHERE id=?", (signal_id,)
        ).fetchone()
    assert row["user_response"] == "APPROVED"
