"""
PAPER auto-approval (2026-09-23): an alerted paper signal becomes a paper
trade without a click, so the paper record measures the system rather than
which alerts got clicked. LIVE must never auto-approve.
"""

import pytest

from tests.test_trade_alert_path import (  # noqa: F401  (pytest fixtures)
    alert_env, ledger, signal, sizing, _run_cycle_with_agree,
)


def _cycle(ledger, signal, monkeypatch, auto: bool):
    _run_cycle_with_agree(ledger, signal, monkeypatch, auto_approve=auto)


def _state(ledger):
    with ledger.get_db() as conn:
        sig = dict(conn.execute("SELECT id, status FROM signals").fetchone())
        trades = [dict(r) for r in conn.execute("SELECT mode, quantity, fill_status FROM trades")]
    return sig, trades


def test_paper_signal_becomes_a_paper_trade(ledger, signal, alert_env, monkeypatch):
    monkeypatch.setenv("PAPER_MODE", "true")
    ga, spy = alert_env
    _cycle(ledger, signal, monkeypatch, auto=True)
    sig, trades = _state(ledger)
    assert sig["status"] == "APPROVED"
    assert len(trades) == 1 and trades[0]["mode"] == "PAPER" and trades[0]["quantity"] == 400
    # one email (the trade alert, flagged as auto-approved) — not two
    subjects = [m["subject"] for m in spy.sent]
    assert not any("Paper trade recorded" in s for s in subjects)
    assert "AUTO-APPROVED" in spy.sent[-1]["html"]


def test_switch_off_leaves_it_pending(ledger, signal, alert_env, monkeypatch):
    monkeypatch.setenv("PAPER_MODE", "true")
    _cycle(ledger, signal, monkeypatch, auto=False)
    sig, trades = _state(ledger)
    assert sig["status"] == "PENDING" and trades == []


def test_auto_approval_refused_in_live(ledger, signal, sizing, alert_env, monkeypatch):
    monkeypatch.setenv("PAPER_MODE", "false")
    ga, _ = alert_env
    sid = ledger.log_signal(signal, sizing, status="PENDING")
    msg, ok, url = ga.handle_email_action("approve", sid, auto=True)
    assert ok is False and url is None and "PAPER" in msg
    sig, trades = _state(ledger)
    assert sig["status"] == "PENDING" and trades == []


def test_human_paper_approval_still_sends_its_confirmation(ledger, signal, sizing, alert_env, monkeypatch):
    monkeypatch.setenv("PAPER_MODE", "true")
    ga, spy = alert_env
    sid = ledger.log_signal(signal, sizing, status="PENDING")
    _, ok, _ = ga.handle_email_action("approve", sid)
    assert ok
    assert any("Paper trade recorded" in m["subject"] for m in spy.sent)
