"""
A QC outage must never again cause a silently-missed trade.

The failure being defended against: the OpenAI account ran out of quota, every
validate_signal() call returned the fail-safe NEEDS_MORE_DATA, run_cycle
blocked every signal, and the daily summary reported "No signals reached the
confidence threshold" — which was false. The candidate cleared the threshold
and died at an infrastructure fault.
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


@pytest.fixture()
def signal():
    from researcher.regime_classifier import Regime
    from researcher.signal_generator import TradeSignal

    return TradeSignal(
        ticker="TITAN", sector="CONSUMER GOODS", exchange="NSE",
        regime=Regime.BULL, strategy_bucket="52wk_breakout", direction="BUY",
        technical_score=88.0, fundamental_score=79.0, confidence_score=84.6,
        rationale="Breakout on 1.9x volume.",
    )


@pytest.fixture()
def sizing():
    from risk_sizer.sizer import SizingDecision

    return SizingDecision(
        approved=True, capital_to_deploy=180_000.0, quantity=400, notes="ok"
    )


def _qc(verdict, errored, rationale="r"):
    from qc_factchecker.validator import QCVerdict

    return QCVerdict(
        verdict=verdict, rationale=rationale,
        disconfirming_evidence_considered="e", errored=errored,
    )


class MailSpy:
    def __init__(self):
        self.sent = []

    def __call__(self, subject="", body="", **kw):
        self.sent.append({"subject": subject, "body": body})
        return True

    def subjects(self):
        return " || ".join(m["subject"] for m in self.sent)


# ---------------------------------------------------------------------------
# The consecutive-error streak
# ---------------------------------------------------------------------------

def test_streak_starts_at_zero(ledger):
    assert ledger.get_qc_error_streak() == 0


def test_streak_increments_and_persists(ledger):
    assert ledger.record_qc_error() == 1
    assert ledger.record_qc_error() == 2
    assert ledger.get_qc_error_streak() == 2


def test_streak_resets_on_a_genuine_verdict(ledger):
    ledger.record_qc_error()
    ledger.record_qc_error()
    ledger.reset_qc_error_streak()
    assert ledger.get_qc_error_streak() == 0


def test_streak_survives_a_reconnect(ledger):
    """
    Persisted in the ledger rather than in memory, so it is not wiped by the
    redeploys that reset every other counter in this system.
    """
    ledger.record_qc_error()
    ledger.record_qc_error()
    ledger.record_qc_error()

    with sqlite3.connect(ledger.DB_PATH) as conn:
        val = conn.execute(
            "SELECT value FROM kv_store WHERE key='qc_error_streak'"
        ).fetchone()
    assert int(val[0]) == 3


# ---------------------------------------------------------------------------
# The immediate alert
# ---------------------------------------------------------------------------

def test_qc_unreachable_alert_names_the_trade_that_was_lost(
    ledger, signal, sizing, monkeypatch
):
    import alerts.gmail_alert as ga

    spy = MailSpy()
    monkeypatch.setattr(ga, "send_plain_email", spy)

    ga.send_qc_unreachable_alert(
        42, signal, sizing,
        _qc("NEEDS_MORE_DATA", True, "QC API error (RateLimitError: 429 quota)"),
        streak=1,
    )

    assert len(spy.sent) == 1
    subject, body = spy.sent[0]["subject"], spy.sent[0]["body"]
    assert "TITAN" in subject
    assert "QC unreachable" in subject
    # everything needed to act on, or at least understand, the loss
    assert "52wk_breakout" in body
    assert "85%" in body or "84" in body
    assert "180,000" in body
    assert "400" in body
    assert "429" in body
    assert "#42" in body or "42" in body


def test_qc_unreachable_alert_offers_no_approve_link(
    ledger, signal, sizing, monkeypatch
):
    """The thesis was never validated, so there is nothing safe to approve."""
    import alerts.gmail_alert as ga

    spy = MailSpy()
    monkeypatch.setattr(ga, "send_plain_email", spy)
    ga.send_qc_unreachable_alert(1, signal, sizing, _qc("NEEDS_MORE_DATA", True), 1)

    body = spy.sent[0]["body"]
    assert "email_action" not in body
    assert "approve" not in body.lower().replace("approve/reject link", "")


def test_qc_down_alert_explains_how_to_diagnose(monkeypatch):
    import alerts.gmail_alert as ga

    spy = MailSpy()
    monkeypatch.setattr(ga, "send_plain_email", spy)
    ga.send_qc_down_alert(3)

    body = spy.sent[0]["body"]
    assert "3" in spy.sent[0]["subject"]
    assert "insufficient_quota" in body
    assert "/status" in body


# ---------------------------------------------------------------------------
# run_cycle's QC gate
# ---------------------------------------------------------------------------

def _run_cycle_to_qc(monkeypatch, ledger, signal, sizing, qc_verdict, spy, send_trade_alert=None):
    """Drive run_cycle with everything upstream of QC stubbed out."""
    import main

    monkeypatch.setattr(main, "PAPER_MODE", True, raising=False)

    import alerts.gmail_alert as ga
    monkeypatch.setattr(ga, "send_plain_email", spy)
    monkeypatch.setattr(ga, "send_trade_alert", send_trade_alert or (lambda *a, **k: True))

    import risk_manager.portfolio_risk as pr

    class Status:
        approved = True
        halt_reason = ""

    monkeypatch.setattr(pr, "check_portfolio_risk", lambda *a, **k: Status())

    import researcher.regime_classifier as rc
    from researcher.regime_classifier import Regime

    class Reading:
        regime = Regime.BULL
        confidence = 80.0
        rationale = "bull"

    monkeypatch.setattr(rc, "classify_regime", lambda *a, **k: Reading())

    import researcher.signal_generator as sg
    monkeypatch.setattr(sg, "generate_signal", lambda *a, **k: signal)

    import risk_sizer.sizer as sz
    monkeypatch.setattr(sz, "size_position", lambda **k: sizing)

    import trader.kite_client as kc
    monkeypatch.setattr(kc, "get_ltp", lambda *a, **k: 450.0)

    import qc_factchecker.validator as qv
    monkeypatch.setattr(qv, "validate_signal", lambda s: qc_verdict)

    main.run_cycle()


def test_errored_qc_sends_an_immediate_alert_and_logs_qc_error(
    ledger, signal, sizing, monkeypatch
):
    spy = MailSpy()
    _run_cycle_to_qc(
        monkeypatch, ledger, signal, sizing,
        _qc("NEEDS_MORE_DATA", True, "QC API error (RateLimitError: 429)"), spy,
    )

    assert "QC unreachable" in spy.subjects()

    with ledger.get_db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT ticker, status FROM signals")]
    assert len(rows) == 1
    assert rows[0]["status"] == "QC_ERROR"
    assert ledger.get_qc_error_streak() == 1


def test_genuine_qc_block_is_quiet_and_logs_qc_blocked(
    ledger, signal, sizing, monkeypatch
):
    """QC doing its job is not an incident. No alarm, but still recorded."""
    spy = MailSpy()
    _run_cycle_to_qc(
        monkeypatch, ledger, signal, sizing,
        _qc("DISAGREE", False, "Volume ratio is 1.1x, needs 1.5x."), spy,
    )

    assert "QC unreachable" not in spy.subjects()

    with ledger.get_db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT status, price_at_signal FROM signals")]
    assert rows[0]["status"] == "QC_BLOCKED"
    assert rows[0]["price_at_signal"] == 450.0  # stubbed get_ltp in _run_cycle_to_qc
    assert ledger.get_qc_error_streak() == 0


def test_genuine_needs_more_data_alerts_instead_of_blocking(
    ledger, signal, sizing, monkeypatch
):
    """
    A DISAGREE means QC found a specific contradicting fact — that stays a
    hard block. NEEDS_MORE_DATA means QC couldn't verify a claim either way,
    which is weaker than a refutation: it should alert like a normal
    candidate, flagged, and leave the decision to the user rather than
    silently discarding every signal whose fundamentals happen to be
    unverifiable online.
    """
    sent_alerts = []

    def _capture_alert(*a, **k):
        sent_alerts.append(k)
        return True

    spy = MailSpy()
    _run_cycle_to_qc(
        monkeypatch, ledger, signal, sizing,
        _qc("NEEDS_MORE_DATA", False, "Could not verify the sentiment claim."), spy,
        send_trade_alert=_capture_alert,
    )

    # Not blocked: a trade alert went out, not silence.
    assert len(sent_alerts) == 1
    risk_flags = sent_alerts[0].get("risk_flags", [])
    assert any("NEEDS_MORE_DATA" in f for f in risk_flags)

    with ledger.get_db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT status, qc_verdict, price_at_signal FROM signals")]
    assert len(rows) == 1
    # Logged like a normal alerted signal (PENDING), not QC_BLOCKED — but the
    # NEEDS_MORE_DATA verdict is still on the row for the weekly audit to
    # analyze separately from true AGREEs.
    assert rows[0]["status"] == "PENDING"
    assert rows[0]["qc_verdict"] == "NEEDS_MORE_DATA"
    assert rows[0]["price_at_signal"] == 450.0


def test_genuine_verdict_resets_a_running_streak(
    ledger, signal, sizing, monkeypatch
):
    ledger.record_qc_error()
    ledger.record_qc_error()

    spy = MailSpy()
    _run_cycle_to_qc(
        monkeypatch, ledger, signal, sizing, _qc("DISAGREE", False), spy
    )
    assert ledger.get_qc_error_streak() == 0


def test_ops_alert_fires_once_at_the_threshold_not_every_cycle(
    ledger, signal, sizing, monkeypatch
):
    errored = _qc("NEEDS_MORE_DATA", True, "429 quota")
    spy = MailSpy()

    for _ in range(5):
        _run_cycle_to_qc(monkeypatch, ledger, signal, sizing, errored, spy)

    down = [m for m in spy.sent if "failed" in m["subject"]]
    per_signal = [m for m in spy.sent if "QC unreachable" in m["subject"]]

    assert len(down) == 1, "ops alert must not repeat every cycle"
    assert "3" in down[0]["subject"], "should fire at the threshold"
    assert len(per_signal) == 5, "every lost trade still gets its own alert"
    assert ledger.get_qc_error_streak() == 5


# ---------------------------------------------------------------------------
# The daily summary must report three distinct outcomes
# ---------------------------------------------------------------------------

def _insert(db, ticker, status, conf=84.0, rationale=None):
    with db.get_db() as conn:
        conn.execute(
            "INSERT INTO signals (created_at, ticker, exchange, regime, "
            "strategy_bucket, direction, technical_score, fundamental_score, "
            "confidence_score, researcher_rationale, qc_rationale, "
            "sized_quantity, capital_to_deploy, sizer_notes, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (db.now_ist(), ticker, "NSE", "bull", "52wk_breakout", "BUY",
             80.0, 70.0, conf, "x", rationale, 400, 180_000.0, "x", status),
        )


def _summary_body(db, monkeypatch):
    import alerts.gmail_alert as ga

    spy = MailSpy()
    monkeypatch.setattr(ga, "send_plain_email", spy)
    ga.send_daily_cycle_summary()
    return spy.sent[0]["body"]


def test_summary_reports_nothing_reached_qc_when_truly_quiet(
    ledger, monkeypatch
):
    body = _summary_body(ledger, monkeypatch)
    assert "No candidate cleared" in body
    assert "QC was not the blocker" in body
    assert "SYSTEM DEGRADED" not in body


def test_summary_separates_a_genuine_qc_block_from_a_quiet_day(
    ledger, monkeypatch
):
    _insert(ledger, "TITAN", "QC_BLOCKED")
    body = _summary_body(ledger, monkeypatch)

    assert "QC reviewed and blocked" in body
    assert "TITAN" in body
    assert "No candidate cleared" not in body
    assert "SYSTEM DEGRADED" not in body


def test_summary_shouts_when_qc_was_unreachable(ledger, monkeypatch):
    """The case that was previously reported as a quiet market."""
    _insert(ledger, "TITAN", "QC_ERROR",
            rationale="QC API error (RateLimitError: 429 insufficient_quota)")
    body = _summary_body(ledger, monkeypatch)

    assert "SYSTEM DEGRADED" in body
    assert "NOT rejections" in body
    assert "insufficient_quota" in body
    assert "No candidate cleared" not in body
    assert "No signals reached the confidence threshold" not in body


def test_summary_never_collapses_the_three_outcomes(ledger, monkeypatch):
    _insert(ledger, "AAA", "QC_ERROR", rationale="429 quota")
    _insert(ledger, "BBB", "QC_BLOCKED")
    _insert(ledger, "CCC", "PENDING")

    body = _summary_body(ledger, monkeypatch)

    assert "SYSTEM DEGRADED" in body and "AAA" in body
    assert "QC reviewed and blocked" in body and "BBB" in body
    assert "Signals alerted today" in body and "CCC" in body


def test_summary_body_is_marked_degraded_only_on_qc_error(
    ledger, monkeypatch
):
    _insert(ledger, "BBB", "QC_BLOCKED")
    assert "[SYSTEM DEGRADED]" not in _summary_body(ledger, monkeypatch)

    _insert(ledger, "AAA", "QC_ERROR", rationale="429")
    assert "[SYSTEM DEGRADED]" in _summary_body(ledger, monkeypatch)
