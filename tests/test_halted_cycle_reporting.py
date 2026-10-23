"""A hard circuit-breaker halt must be logged and reported as a halt, never as
'SYSTEM DEGRADED - no cycle evaluations' (2026-10-22/23), and the halt email
must not repeat every cycle."""
import os
import sqlite3
import tempfile
from contextlib import closing
from types import SimpleNamespace

import pytest

from alerts.gmail_alert import assess_cycle_health, is_halt_day

WEEKLY = ("WEEKLY LOSS CIRCUIT BREAKER: week P&L is ₹-23,427 (limit: ₹-15,000). "
          "Halting for the rest of the week.")


@pytest.fixture()
def ledger(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    import ledger.db as db
    monkeypatch.setattr(db, "DB_PATH", path)
    with closing(sqlite3.connect(path)) as conn:
        with open(os.path.join(os.path.dirname(db.__file__), "schema.sql")) as f:
            conn.executescript(f.read())
    yield db
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(path + suffix)
        except OSError:
            pass


def _h(t, rationale=WEEKLY):
    return {"verdict": "HALTED", "cycle_at": t, "rationale": rationale,
            "regime": "unknown", "regime_confidence": 0.0}


# ---- summary wording -------------------------------------------------------
def test_halt_only_day_is_not_degraded_and_says_halted():
    rows = [_h(f"2026-10-23 {h:02d}:15:00") for h in range(9, 16)]
    block, degraded = assess_cycle_health(rows, 0)
    assert not degraded
    assert "HALTED BY CIRCUIT BREAKER" in block and "7 cycle(s)" in block
    assert "WEEKLY LOSS" in block and "not a system fault" in block
    assert "NO CYCLE EVALUATIONS" not in block


def test_empty_log_is_still_degraded():
    block, degraded = assess_cycle_health([], 0)
    assert degraded and "NO CYCLE EVALUATIONS" in block


def test_halt_plus_errors_still_flags_errors():
    rows = [_h("2026-10-23 09:15:00")] + [
        {"verdict": "ERROR", "cycle_at": "2026-10-23 10:15:00", "rationale": "boom"}] * 4
    block, degraded = assess_cycle_health(rows, 0)
    assert degraded and "PIPELINE ERRORS" in block and "HALTED" in block


def test_halted_cycles_count_as_ran_after_auth_skip():
    rows = [_h("2026-10-23 10:15:00"), _h("2026-10-23 11:15:00")]
    block, degraded = assess_cycle_health(rows, 1, "bad token")
    assert not degraded and "NO RESEARCH RAN" not in block and "HALTED" in block


def test_is_halt_day():
    assert not is_halt_day([])
    assert is_halt_day([_h("t")])
    assert not is_halt_day([_h("t"), {"verdict": "PASS", "cycle_at": "t"}])


# ---- HALTED row written by run_cycle --------------------------------------
def test_run_cycle_logs_halted_row_and_stops(ledger, monkeypatch):
    import main
    import risk_manager.portfolio_risk as pr
    import universe.loader as ul
    import researcher.signal_generator as sg
    monkeypatch.setattr(main, "PAPER_MODE", True, raising=False)
    monkeypatch.setattr(pr, "CAPITAL", 1_000_000.0)
    monkeypatch.setattr(pr, "_send_halt_alert", lambda s: None)
    monkeypatch.setattr(ledger, "get_all_time_pnl", lambda mode: -23_427.0)
    monkeypatch.setattr(ledger, "get_open_positions", lambda mode=None: [])
    monkeypatch.setattr(ledger, "get_weekly_pnl", lambda mode=None: -23_427.0)
    monkeypatch.setattr(ul, "load_scan_universe", lambda: [])
    reached = []
    monkeypatch.setattr(sg, "generate_signal", lambda *a, **k: reached.append(1) or None)

    main.run_cycle()

    assert not reached
    rows = ledger.get_cycle_log(days=1)
    assert len(rows) == 1
    assert rows[0]["verdict"] == "HALTED"
    assert rows[0]["strategy"] == "halt:WEEKLY_LOSS"
    assert "WEEKLY LOSS" in rows[0]["rationale"]


def test_halted_row_logging_never_raises(monkeypatch):
    import main
    import ledger.db as db
    def boom(**k):
        raise RuntimeError("db down")
    monkeypatch.setattr(db, "log_cycle_evaluation", boom)
    main._log_halted_cycle(SimpleNamespace(halt_reason="x", halt_kind="WEEKLY_LOSS"))


# ---- halt email dedupe -----------------------------------------------------
def _status(kind="WEEKLY_LOSS"):
    return SimpleNamespace(halt_kind=kind)


def test_halt_email_sent_once_per_day(ledger, monkeypatch):
    import risk_manager.portfolio_risk as pr
    sent = []
    monkeypatch.setattr(pr, "_send_halt_alert", lambda s: sent.append(s.halt_kind))
    for _ in range(7):
        pr._maybe_send_halt_alert(_status(), "PAPER")
    assert sent == ["WEEKLY_LOSS"]


def test_halt_email_resent_when_breaker_kind_changes(ledger, monkeypatch):
    import risk_manager.portfolio_risk as pr
    sent = []
    monkeypatch.setattr(pr, "_send_halt_alert", lambda s: sent.append(s.halt_kind))
    pr._maybe_send_halt_alert(_status("WEEKLY_LOSS"), "PAPER")
    pr._maybe_send_halt_alert(_status("DRAWDOWN"), "PAPER")
    pr._maybe_send_halt_alert(_status("DRAWDOWN"), "PAPER")
    assert sent == ["WEEKLY_LOSS", "DRAWDOWN"]


def test_halt_email_dedupe_is_per_book(ledger, monkeypatch):
    import risk_manager.portfolio_risk as pr
    sent = []
    monkeypatch.setattr(pr, "_send_halt_alert", lambda s: sent.append(1))
    pr._maybe_send_halt_alert(_status(), "PAPER")
    pr._maybe_send_halt_alert(_status(), "LIVE")
    assert len(sent) == 2


def test_halt_email_sends_if_dedupe_store_fails(monkeypatch):
    import risk_manager.portfolio_risk as pr
    import ledger.db as db
    sent = []
    monkeypatch.setattr(pr, "_send_halt_alert", lambda s: sent.append(1))
    monkeypatch.setattr(db, "kv_get", lambda k: (_ for _ in ()).throw(RuntimeError("x")))
    def bad_set(k, v):
        raise RuntimeError("x")
    monkeypatch.setattr(db, "kv_set", bad_set)
    pr._maybe_send_halt_alert(_status(), "PAPER")
    pr._maybe_send_halt_alert(_status(), "PAPER")
    assert len(sent) == 2
