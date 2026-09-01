"""
Smoke test: the weekly Auditor's data assembly must not choke once
signal_shadow_checks exist, and must actually include them so the model has
real graded outcomes to work from — not just the researcher/QC narrative.
"""

import json
import os
import sqlite3
import sys
import tempfile
import types
from datetime import date, timedelta

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


def _monkeypatch_gemini(monkeypatch, captured):
    """Stub google.generativeai so no network call happens; capture the
    prompt text sent so the test can assert on its contents."""
    fake_module = types.ModuleType("google.generativeai")

    class _Resp:
        text = json.dumps({
            "summary": "ok", "confidence_bucket_analysis": {},
            "missed_opportunities": "ok", "hypothesis_backlog": [],
            "calibration_flag": False,
        })

    class _Model:
        def __init__(self, *a, **k):
            pass

        def generate_content(self, prompt):
            captured["prompt"] = prompt
            return _Resp()

    fake_module.configure = lambda **k: None
    fake_module.GenerativeModel = _Model

    fake_google = types.ModuleType("google")
    fake_google.generativeai = fake_module
    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.generativeai", fake_module)


def test_weekly_audit_includes_shadow_checks_for_blocked_signals(ledger, monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-key-for-test")

    today = date.today()
    week_start = (today - timedelta(days=today.weekday())).strftime("%Y-%m-%d")

    with ledger.get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO signals (
                created_at, ticker, exchange, regime, strategy_bucket, direction,
                technical_score, fundamental_score, confidence_score,
                researcher_rationale, qc_verdict, qc_rationale, status, price_at_signal
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (f"{week_start} 10:15:00", "HEG", "NSE", "bull", "52wk_breakout", "BUY",
             90.0, 60.0, 78.0, "breakout thesis", "DISAGREE",
             "Volume ratio does not support the breakout claim.", "QC_BLOCKED", 100.0),
        )
        signal_id = cur.lastrowid

    ledger.record_shadow_check(signal_id, horizon_days=1, price_at_check=110.0, return_pct=10.0)

    captured = {}
    _monkeypatch_gemini(monkeypatch, captured)

    from auditor.weekly_audit import run_weekly_audit
    run_weekly_audit()  # must not raise

    assert "prompt" in captured, "Gemini was never called — audit must not skip when data exists"
    assert "SHADOW_PRICE_CHECKS" in captured["prompt"]
    assert '"return_pct": 10.0' in captured["prompt"]
    assert "HEG" in captured["prompt"]

    with ledger.get_db() as conn:
        row = conn.execute("SELECT * FROM weekly_audits").fetchone()
    assert row is not None, "audit result must still be written to the ledger"
