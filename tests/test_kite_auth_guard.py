"""Kite auth failures must be loud at 09:15, and never reported as a quiet day."""
import os
import sqlite3
import tempfile

import pytest

from alerts.gmail_alert import assess_cycle_health


@pytest.fixture()
def ledger(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    import ledger.db as db
    monkeypatch.setattr(db, "DB_PATH", path)
    with sqlite3.connect(path) as conn:
        with open(os.path.join(os.path.dirname(db.__file__), "schema.sql")) as f:
            conn.executescript(f.read())
    yield db
    os.unlink(path)


def _row(verdict, rationale=""):
    return {"verdict": verdict, "rationale": rationale}


# ---- assess_cycle_health -------------------------------------------------
def test_healthy_quiet_day_is_not_flagged():
    rows = [_row("PREFILTER_SKIP")] * 50 + [_row("PASS")] * 5
    assert assess_cycle_health(rows, 0) == (None, False)


def test_preflight_auth_failures_flag_login_invalid():
    block, degraded = assess_cycle_health([], 3, "Incorrect `api_key` or `access_token`.")
    assert degraded and "KITE LOGIN INVALID" in block and "access_token" in block


def test_auth_errors_in_cycle_rows_flag_login_invalid():
    rows = [_row("ERROR", "Incorrect `api_key` or `access_token`.")] * 3
    block, degraded = assess_cycle_health(rows, 0)
    assert degraded and "KITE LOGIN INVALID" in block


def test_high_error_share_flags_pipeline_errors():
    rows = [_row("ERROR", "boom")] * 3 + [_row("PREFILTER_SKIP")] * 7
    block, degraded = assess_cycle_health(rows, 0)
    assert degraded and "PIPELINE ERRORS" in block


def test_low_error_share_is_tolerated():
    rows = [_row("ERROR", "one-off")] + [_row("PREFILTER_SKIP")] * 20
    assert assess_cycle_health(rows, 0) == (None, False)


def test_no_rows_at_all_is_flagged_not_called_quiet():
    block, degraded = assess_cycle_health([], 0)
    assert degraded and "NO CYCLE EVALUATIONS" in block


# ---- preflight -----------------------------------------------------------
class _Mail:
    def __init__(self):
        self.sent = []

    def __call__(self, count_reason_pair=None, *a, **k):
        self.sent.append(1)


def _patch_auth(monkeypatch, ok, reason="Incorrect `api_key` or `access_token`."):
    import trader.kite_preflight as kp
    monkeypatch.setattr(kp, "check_kite_auth", lambda: (ok, reason))
    sent = []
    import alerts.gmail_alert as ga
    monkeypatch.setattr(ga, "send_kite_auth_failed_alert",
                        lambda reason, count: sent.append(count) or True)
    return kp, sent


def test_preflight_ok_runs_cycle_and_sends_nothing(ledger, monkeypatch):
    kp, sent = _patch_auth(monkeypatch, True)
    assert kp.preflight_ok_or_alert() is True
    assert sent == []


def test_preflight_failure_skips_cycle_and_alerts_on_1st_and_4th_only(ledger, monkeypatch):
    kp, sent = _patch_auth(monkeypatch, False)
    results = [kp.preflight_ok_or_alert() for _ in range(7)]
    assert results == [False] * 7
    assert sent == [1, 4]
    assert kp.get_auth_failures()[0] == 7


def test_is_auth_error_only_matches_login_problems():
    import trader.kite_preflight as kp
    assert kp.is_auth_error(Exception("Incorrect `api_key` or `access_token`."))
    assert kp.is_auth_error(EnvironmentError("No Kite access_token found."))
    assert not kp.is_auth_error(Exception("Read timed out"))
    assert not kp.is_auth_error(Exception("502 Bad Gateway"))


def test_network_blip_does_not_block_cycle(monkeypatch):
    import trader.kite_client as kc
    import trader.kite_preflight as kp

    class _K:
        def profile(self):
            raise Exception("Read timed out")

    monkeypatch.setattr(kc, "get_kite_client", lambda: _K())
    ok, _ = kp.check_kite_auth()
    assert ok is True


def test_guard_bug_never_blocks_cycle(monkeypatch):
    import trader.kite_preflight as kp

    def boom():
        raise RuntimeError("guard bug")

    monkeypatch.setattr(kp, "check_kite_auth", boom)
    assert kp.preflight_ok_or_alert() is True


def test_summary_email_says_login_invalid_not_quiet(ledger, monkeypatch):
    import alerts.gmail_alert as ga
    import trader.kite_preflight as kp
    kp.record_auth_failure("Incorrect `api_key` or `access_token`.")
    sent = []
    monkeypatch.setattr(ga, "send_plain_email",
                        lambda subject, body: sent.append((subject, body)) or True)
    ga.send_daily_cycle_summary()
    subject, body = sent[0]
    assert "SYSTEM DEGRADED" in subject
    assert "KITE LOGIN INVALID" in body
    assert "[SYSTEM DEGRADED]" in body
