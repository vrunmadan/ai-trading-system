"""
The happy path: QC says AGREE and an actionable alert reaches the user.

Every other test in this area covers a way the pipeline blocks. This one
covers the case that has to keep working — a signal clearing every gate and
producing an email with the REAL sized quantity and links that actually
verify. Two separate bugs have already lived on this path (the quantity was
always 0/1, and the ledger row was never written), so it is pinned here.
"""

import os
import re
import sqlite3
import tempfile
import urllib.parse as urlparse

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
        rationale="Breakout within 0.4% of the 52wk high on 1.9x volume.",
    )


@pytest.fixture()
def sizing():
    """450/share against Rs 180,000 approved = 400 shares."""
    from risk_sizer.sizer import SizingDecision

    return SizingDecision(
        approved=True, capital_to_deploy=180_000.0, quantity=400,
        notes="Approved Rs 180,000 (18.0% of capital).",
    )


class EmailSpy:
    def __init__(self):
        self.sent = []

    def __call__(self, subject, html_body, plain_body=""):
        self.sent.append(
            {"subject": subject, "html": html_body, "plain": plain_body}
        )
        return True


def _agree():
    from qc_factchecker.validator import QCVerdict

    return QCVerdict(
        verdict="AGREE",
        rationale="Volume and RSI check out against the stated rules.",
        disconfirming_evidence_considered="Checked whether the run was already extended.",
        errored=False,
    )


@pytest.fixture()
def alert_env(monkeypatch):
    monkeypatch.setenv("APPROVAL_SECRET", "test-approval-secret")
    monkeypatch.setenv("KITE_API_KEY", "test-kite-key")
    monkeypatch.setenv("RESEND_API_KEY", "test-resend")
    monkeypatch.setenv("ALERT_EMAIL", "trader@example.com")
    monkeypatch.setenv("RAILWAY_URL", "https://trading.example.com")

    import importlib

    import alerts.gmail_alert as ga
    importlib.reload(ga)          # module-level config is read at import
    spy = EmailSpy()
    monkeypatch.setattr(ga, "_send_email", spy)
    yield ga, spy
    importlib.reload(ga)


# ---------------------------------------------------------------------------
# The alert itself
# ---------------------------------------------------------------------------

def test_alert_shows_the_real_share_count(ledger, signal, sizing, alert_env):
    """
    Regression: the email rendered "Quantity: 0 shares" next to a five-figure
    capital number, because the sizer's placeholder was never filled in.
    """
    ga, spy = alert_env
    assert ga.send_trade_alert(1, signal, _agree(), sizing) is True

    html = spy.sent[0]["html"]
    # Match the rendered cell, not a bare substring: "400 shares" contains
    # "0 shares", so a naive negative assertion here would be meaningless.
    qty_cells = re.findall(r">(\d+) shares<", html)
    assert qty_cells == ["400"], f"expected one 400-share cell, got {qty_cells}"
    assert "180,000" in html


def test_alert_basket_url_carries_the_real_quantity(
    ledger, signal, sizing, alert_env
):
    ga, spy = alert_env
    ga.send_trade_alert(1, signal, _agree(), sizing)

    html = spy.sent[0]["html"]
    m = re.search(r'https://kite\.zerodha\.com/connect/basket\?[^"\']+', html)
    assert m, "no Kite basket URL in the alert"

    data = urlparse.parse_qs(urlparse.urlparse(m.group(0)).query).get("data", [""])[0]
    assert '"quantity": 400' in data or '"quantity":400' in data
    assert "TITAN" in data


def test_alert_carries_approve_and_reject_links(
    ledger, signal, sizing, alert_env
):
    ga, spy = alert_env
    ga.send_trade_alert(7, signal, _agree(), sizing)

    html = spy.sent[0]["html"]
    assert "action=approve&id=7" in html
    assert "action=reject&id=7" in html


def test_those_links_actually_verify(ledger, signal, sizing, alert_env):
    """
    A link that does not verify is worse than no link: the user taps it during
    market hours and gets a 403 with the opportunity gone.
    """
    ga, spy = alert_env
    ga.send_trade_alert(7, signal, _agree(), sizing)
    html = spy.sent[0]["html"]

    for action in ("approve", "reject"):
        m = re.search(rf"action={action}&id=7&token=([a-f0-9]+)", html)
        assert m, f"no {action} token in the alert"
        assert ga.verify_token(action, 7, m.group(1)) is True

    # and a tampered token must not
    m = re.search(r"action=approve&id=7&token=([a-f0-9]+)", html)
    bad = ("0" if m.group(1)[0] != "0" else "1") + m.group(1)[1:]
    assert ga.verify_token("approve", 7, bad) is False


def test_approve_link_for_one_signal_does_not_work_on_another(
    ledger, signal, sizing, alert_env
):
    ga, spy = alert_env
    ga.send_trade_alert(7, signal, _agree(), sizing)
    html = spy.sent[0]["html"]
    token = re.search(r"action=approve&id=7&token=([a-f0-9]+)", html).group(1)

    assert ga.verify_token("approve", 8, token) is False
    assert ga.verify_token("reject", 7, token) is False


def test_alert_includes_the_qc_verdict(ledger, signal, sizing, alert_env):
    ga, spy = alert_env
    ga.send_trade_alert(1, signal, _agree(), sizing)
    html = spy.sent[0]["html"]
    assert "AGREE" in html
    assert "Volume and RSI check out" in html


# ---------------------------------------------------------------------------
# End to end through run_cycle
# ---------------------------------------------------------------------------

def test_agree_verdict_produces_an_actionable_alert_end_to_end(
    ledger, signal, sizing, alert_env, monkeypatch
):
    """
    QC agrees, so a signal row is written as PENDING and a real alert goes out
    with the quantity derived from the live price.
    """
    ga, spy = alert_env
    import main

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
    from risk_sizer.sizer import SizingDecision

    monkeypatch.setattr(
        sz, "size_position",
        lambda **k: SizingDecision(True, 180_000.0, 0, "approved"),
    )

    import trader.kite_client as kc
    monkeypatch.setattr(kc, "get_ltp", lambda *a, **k: 450.0)

    import qc_factchecker.validator as qv
    monkeypatch.setattr(qv, "validate_signal", lambda s: _agree())

    main.run_cycle()

    # a signal row exists and is awaiting a response
    with ledger.get_db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT ticker, status, sized_quantity FROM signals")]
    assert len(rows) == 1
    assert rows[0]["status"] == "PENDING"
    assert rows[0]["sized_quantity"] == 400      # 180000 // 450, not 0 and not 1

    # and the alert that went out is actionable
    assert spy.sent, "no trade alert was sent"
    html = spy.sent[-1]["html"]
    assert "400 shares" in html
    assert "action=approve" in html and "action=reject" in html


def test_agree_does_not_trip_the_qc_error_streak(
    ledger, signal, sizing, alert_env, monkeypatch
):
    ledger.record_qc_error()
    ledger.record_qc_error()

    ga, spy = alert_env
    import main

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
    monkeypatch.setattr(qv, "validate_signal", lambda s: _agree())

    main.run_cycle()

    assert ledger.get_qc_error_streak() == 0
