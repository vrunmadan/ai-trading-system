"""
get_weekly_pnl()'s week-start boundary used to be computed by SQLite as
date('now', 'weekday 1', '-7 days'). SQLite's "weekday 1" modifier is a
no-op when 'now' already falls on a Monday, so the unconditional "-7 days"
that followed landed on the PREVIOUS Monday instead of today — reproduced
running on Monday 21 September 2026, which selected 14 September. A trade
closed earlier that same Monday morning would have been excluded from "this
week's" P&L, so the weekly-loss circuit breaker could understate a losing
week specifically on the one day it matters most for a fresh week's limit.

It also measured "now" in UTC (SQLite's default), not IST — the same class
of bug already fixed elsewhere in this codebase for get_cycle_log(). Both
are fixed by computing the boundary explicitly from datetime.now(IST) in
Python (2026-09-19 review, item 4).

These tests freeze "now" by monkeypatching the `datetime` name inside
ledger.db, since the actual day of the week the suite runs on can't be
controlled otherwise.
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


def _freeze_now(monkeypatch, db, moment):
    """Make db.datetime.now(...) always return `moment` (an IST-aware datetime)."""

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment

    monkeypatch.setattr(db, "datetime", _Frozen)


def _insert_closed_trade(db, exit_time, pnl, mode="PAPER", ticker="ACME"):
    with db.get_db() as conn:
        conn.execute(
            """
            INSERT INTO trades (ticker, direction, quantity, entry_price,
                                entry_time, exit_price, exit_time, pnl, mode)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (ticker, "BUY", 100, 450.0, exit_time, 460.0, exit_time, pnl, mode),
        )


def test_monday_run_includes_a_trade_closed_earlier_the_same_monday(ledger, monkeypatch):
    """
    The exact reproduction from the review: running ON a Monday must count
    a trade closed earlier THAT SAME Monday. The old
    date('now','weekday 1','-7 days') logic pushed the boundary back to the
    PREVIOUS Monday on this one day of the week, excluding it.
    """
    db = ledger
    monday_10am = IST.localize(datetime(2026, 9, 21, 10, 0, 0))  # a real Monday
    _freeze_now(monkeypatch, db, monday_10am)

    _insert_closed_trade(db, exit_time="2026-09-21 09:00:00", pnl=500.0)

    assert db.get_weekly_pnl() == pytest.approx(500.0)
    assert db.get_weekly_pnl(mode="PAPER") == pytest.approx(500.0)


def test_monday_run_excludes_a_trade_from_the_previous_week(ledger, monkeypatch):
    """A trade closed the Friday before this Monday belongs to last week."""
    db = ledger
    monday_10am = IST.localize(datetime(2026, 9, 21, 10, 0, 0))
    _freeze_now(monkeypatch, db, monday_10am)

    _insert_closed_trade(db, exit_time="2026-09-18 15:00:00", pnl=-9_000.0)  # last Friday

    assert db.get_weekly_pnl() == pytest.approx(0.0)


def test_midweek_run_includes_mondays_trade_and_excludes_last_mondays(ledger, monkeypatch):
    """
    Sanity check on a non-edge day: a Wednesday run must reach back to this
    week's Monday but no further.
    """
    db = ledger
    wednesday_noon = IST.localize(datetime(2026, 9, 23, 12, 0, 0))
    _freeze_now(monkeypatch, db, wednesday_noon)

    _insert_closed_trade(db, exit_time="2026-09-21 09:00:00", pnl=500.0, ticker="THISWEEK")
    _insert_closed_trade(db, exit_time="2026-09-14 09:00:00", pnl=-8_000.0, ticker="LASTWEEK")

    assert db.get_weekly_pnl() == pytest.approx(500.0)


def test_mode_filter_still_applies_after_the_boundary_fix(ledger, monkeypatch):
    db = ledger
    monday_10am = IST.localize(datetime(2026, 9, 21, 10, 0, 0))
    _freeze_now(monkeypatch, db, monday_10am)

    _insert_closed_trade(db, exit_time="2026-09-21 09:00:00", pnl=500.0, mode="PAPER")
    _insert_closed_trade(db, exit_time="2026-09-21 09:30:00", pnl=-200.0, mode="LIVE")

    assert db.get_weekly_pnl(mode="PAPER") == pytest.approx(500.0)
    assert db.get_weekly_pnl(mode="LIVE") == pytest.approx(-200.0)
    assert db.get_weekly_pnl() == pytest.approx(300.0)
