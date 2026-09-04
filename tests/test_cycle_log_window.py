"""
get_cycle_log()'s day-window used SQLite's datetime('now', '-N days'), which
returns UTC, compared as a raw string against cycle_at (a naive IST
wall-clock string — see now_ist()). Since IST is UTC+5:30, that comparison
understates the cutoff by 5.5 hours, which OVERSTATES the actual lookback:
a "days=1" window reached back ~29.5 real hours, not 24.

This is exactly how RBLBANK's signal from ~27 hours earlier (well past
"today" by any real calendar) still showed up in a "Top 5 scores today"
table the following afternoon: it hadn't aged out of the oversized window,
even though no new evaluation had actually happened. The per-signal alert
emails were unaffected because they're sourced from a correctly-scoped
DATE(created_at) query on the `signals` table, not this function — which is
exactly why the daily summary and the actual inbox disagreed.
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


def _insert_cycle_row(db, ticker, hours_ago, confidence_score=80.0, verdict="TRADE"):
    cycle_at = (datetime.now(IST) - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")
    with db.get_db() as conn:
        conn.execute(
            """
            INSERT INTO cycle_log (
                cycle_at, regime, regime_confidence, ticker, exchange, strategy,
                verdict, technical_score, fundamental_score, confidence_score, rationale
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (cycle_at, "sideways", 60.0, ticker, "NSE", "bb_squeeze_break",
             verdict, 84.0, 73.0, confidence_score, "r"),
        )


def test_a_signal_from_27_hours_ago_is_not_todays_top_5(ledger):
    """
    The exact RBLBANK scenario: a real evaluation from ~27 hours ago (i.e.
    yesterday afternoon, relative to a "days=1" call made this afternoon)
    must not be reported as part of "today." The old UTC-vs-IST bug let it
    through because 27h < its inflated ~29.5h effective window.
    """
    _insert_cycle_row(ledger, "RBLBANK", hours_ago=27.28)  # 27h17m, like the real case
    rows = ledger.get_cycle_log(days=1)
    assert rows == []


def test_a_signal_from_20_hours_ago_is_correctly_included(ledger):
    """A genuinely-within-the-last-24h row must still show up."""
    _insert_cycle_row(ledger, "ACMESOLAR", hours_ago=20)
    rows = ledger.get_cycle_log(days=1)
    assert len(rows) == 1
    assert rows[0]["ticker"] == "ACMESOLAR"


def test_a_signal_from_30_hours_ago_is_excluded(ledger):
    """Comfortably past even the old buggy ~29.5h window — must stay excluded."""
    _insert_cycle_row(ledger, "OLDTICKER", hours_ago=30)
    rows = ledger.get_cycle_log(days=1)
    assert rows == []


def test_window_is_close_to_24_hours_not_29_5(ledger):
    """
    Directly pins the fix: a row at 23.9h must be in, a row at 24.1h must be
    out. The old bug's effective boundary sat around 29.5h instead.
    """
    _insert_cycle_row(ledger, "JUSTIN", hours_ago=23.9)
    _insert_cycle_row(ledger, "JUSTOUT", hours_ago=24.1)
    rows = ledger.get_cycle_log(days=1)
    tickers = {r["ticker"] for r in rows}
    assert tickers == {"JUSTIN"}
