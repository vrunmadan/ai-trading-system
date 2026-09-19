"""
Tests for monitor.position_monitor's "price-fetch failures must alert
instead of silently skipping checks" fix (2026-09-19 review, item 6).

Before this fix, a Kite-connection failure (blocking every open position)
logged an error and returned, and a per-position LTP failure logged an
error and moved to the next position — either way, only the logs (which
nobody watches day to day) recorded it. If every position failed to price,
the monitor's own "no alerts" branch would report a clean, all-nominal day
that never actually happened.
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


def _open_trade(db, ticker="ACME", exchange="NSE", quantity=400,
                entry_price=450.0, mode="PAPER"):
    with db.get_db() as conn:
        conn.execute(
            """
            INSERT INTO trades (ticker, direction, quantity, entry_price,
                                entry_time, mode, exchange, fill_status)
            VALUES (?,?,?,?,?,?,?,?)
            """,
            (ticker, "BUY", quantity, entry_price, db.now_ist(), mode,
             exchange, "CONFIRMED"),
        )


def _capture_email(monkeypatch):
    import monitor.position_monitor as pm

    sent: list[str] = []
    monkeypatch.setattr(pm, "_send_monitor_email", lambda text: sent.append(text))
    return sent


def _stub_regime(monkeypatch, regime="bull"):
    class _Reading:
        class regime:
            value = regime

    import researcher.regime_classifier as rc

    monkeypatch.setattr(rc, "classify_regime", lambda **kw: _Reading())


def test_kite_unreachable_sends_an_alert_instead_of_silently_skipping(ledger, monkeypatch):
    db = ledger
    _open_trade(db)
    sent = _capture_email(monkeypatch)
    _stub_regime(monkeypatch)

    import trader.kite_client as kc

    monkeypatch.setattr(
        kc, "get_kite_client",
        lambda: (_ for _ in ()).throw(RuntimeError("no token")),
    )

    import monitor.position_monitor as pm
    pm.check_open_positions()

    assert len(sent) == 1
    assert "not run" in sent[0].lower()
    assert "kite" in sent[0].lower()


def test_a_single_positions_price_fetch_failure_reaches_the_alert(ledger, monkeypatch):
    """
    Two open positions: one nominal, one whose LTP fetch fails. Before this
    fix, the failing one was silently skipped and never reached the email —
    only the nominal position (which generates no alert of its own) would
    have been reflected, i.e. as a false "no alerts" day.
    """
    db = ledger
    _open_trade(db, ticker="GOODCO", entry_price=100.0)
    _open_trade(db, ticker="BADCO", entry_price=200.0)
    sent = _capture_email(monkeypatch)
    _stub_regime(monkeypatch)

    class PartialFailKite:
        def ltp(self, key):
            if "BADCO" in key:
                raise RuntimeError("quote unavailable")
            return {key: {"last_price": 105.0}}  # GOODCO: up 5%, nothing to flag

    import trader.kite_client as kc
    monkeypatch.setattr(kc, "get_kite_client", lambda: PartialFailKite())

    import monitor.position_monitor as pm
    pm.check_open_positions()

    assert len(sent) == 1
    assert "BADCO" in sent[0]
    assert "PRICE FETCH FAILED" in sent[0]
    assert "GOODCO" not in sent[0]


def test_every_position_failing_to_price_does_not_report_a_clean_day(ledger, monkeypatch):
    """The exact failure mode the review names: total LTP failure must not
    fall through to "all nominal — no alerts."""
    db = ledger
    _open_trade(db, ticker="ACME")
    sent = _capture_email(monkeypatch)
    _stub_regime(monkeypatch)

    class AllFailKite:
        def ltp(self, key):
            raise RuntimeError("quote unavailable")

    import trader.kite_client as kc
    monkeypatch.setattr(kc, "get_kite_client", lambda: AllFailKite())

    import monitor.position_monitor as pm
    pm.check_open_positions()

    assert len(sent) == 1
    assert "PRICE FETCH FAILED" in sent[0]
    assert "ACME" in sent[0]


def test_nominal_positions_still_produce_no_alert(ledger, monkeypatch):
    """Sanity check: this fix must not turn every ordinary day into an alert."""
    db = ledger
    _open_trade(db, ticker="ACME", entry_price=450.0)
    sent = _capture_email(monkeypatch)
    _stub_regime(monkeypatch)

    class NominalKite:
        def ltp(self, key):
            return {key: {"last_price": 460.0}}

    import trader.kite_client as kc
    monkeypatch.setattr(kc, "get_kite_client", lambda: NominalKite())

    import monitor.position_monitor as pm
    pm.check_open_positions()

    assert sent == []
