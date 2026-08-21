"""
Provider health checks.

/status used to report "ok" while the QC fact-checker was failing every call,
because it only checked whether keys were SET. A present, valid, out-of-money
key is indistinguishable from a healthy one until you actually call the API.
"""

import pytest

from monitor import provider_health as ph


@pytest.fixture(autouse=True)
def clear_cache():
    ph.reset_cache()
    yield
    ph.reset_cache()


class _Err(Exception):
    def __init__(self, msg, status_code=None):
        super().__init__(msg)
        self.status_code = status_code


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("msg, status, expected", [
    ("Error code: 429 - {'error': {'message': 'You exceeded your current quota', "
     "'type': 'insufficient_quota'}}", 429, "insufficient_quota"),
    ("invalid_api_key: Incorrect API key provided", 401, "invalid_api_key"),
    ("The model `gpt-9` does not exist", 404, "model_not_found"),
    ("Rate limit reached for requests", 429, "rate_limited"),
    ("Request timed out", None, "timeout"),
    ("Internal server error", 500, "provider_error"),
    ("Connection error while resolving DNS", None, "unreachable"),
    ("your credit balance is too low", None, "billing"),
    ("something entirely novel", None, "unknown_error"),
])
def test_error_classes(msg, status, expected):
    assert ph._classify(_Err(msg, status)) == expected


def test_quota_is_distinguished_from_a_plain_rate_limit():
    """
    Both are HTTP 429, but one is 'add money' and the other is 'slow down'.
    Conflating them would send you chasing the wrong fix.
    """
    quota = ph._classify(_Err("429 insufficient_quota", 429))
    throttle = ph._classify(_Err("Rate limit reached for requests", 429))
    assert quota == "insufficient_quota"
    assert throttle == "rate_limited"
    assert quota != throttle


# ---------------------------------------------------------------------------
# Secrets must never leak into a health report
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("secret", [
    "sk-proj-abcdef1234567890abcdef",
    "sk-abcdef1234567890",
    "AIzaSyA1b2C3d4E5f6G7h8I9j0",
])
def test_sanitize_strips_credential_shaped_text(secret):
    out = ph._sanitize(f"Auth failed for key {secret} on request")
    assert secret not in out
    assert "[REDACTED]" in out


def test_bearer_tokens_are_stripped():
    out = ph._sanitize("Authorization: Bearer abc123def456ghi789")
    assert "abc123def456ghi789" not in out


def test_probe_failure_detail_never_contains_the_key(monkeypatch):
    key = "sk-proj-supersecretvalue1234567890"
    monkeypatch.setenv("OPENAI_API_KEY", key)

    import openai

    class Boom:
        def __init__(self, *a, **kw):
            raise _Err(f"Invalid key {key} rejected", 401)

    monkeypatch.setattr(openai, "OpenAI", Boom)

    r = ph.check_provider("openai", force=True)
    assert r["ok"] is False
    assert r["error_class"] == "invalid_api_key"
    assert key not in str(r)


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------

def _fake_openai(monkeypatch, exc=None):
    import openai

    class FakeCompletions:
        def create(self, **kw):
            if exc:
                raise exc
            return object()

    class FakeClient:
        def __init__(self, *a, **kw):
            self.chat = type("c", (), {"completions": FakeCompletions()})()

    monkeypatch.setattr(openai, "OpenAI", FakeClient)


def test_unset_key_reports_not_configured(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    r = ph.check_provider("openai", force=True)
    assert r["ok"] is False
    assert r["configured"] is False
    assert r["error_class"] == "not_configured"


def test_healthy_provider_reports_ok(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    _fake_openai(monkeypatch)
    r = ph.check_provider("openai", force=True)
    assert r["ok"] is True
    assert r["critical"] is True
    assert r["role"] == "QC fact-checker"


def test_out_of_quota_provider_reports_the_live_failure(monkeypatch):
    """The actual production failure."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    _fake_openai(monkeypatch, exc=_Err(
        "Error code: 429 - {'error': {'type': 'insufficient_quota'}}", 429))

    r = ph.check_provider("openai", force=True)
    assert r["ok"] is False
    assert r["error_class"] == "insufficient_quota"


def test_a_probe_that_explodes_does_not_take_down_the_check(monkeypatch):
    monkeypatch.setattr(ph, "_PROBES", dict(ph._PROBES))
    ph._PROBES["openai"] = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    r = ph.check_provider("openai", force=True)
    assert r["ok"] is False
    assert r["error_class"] == "probe_failed"


# ---------------------------------------------------------------------------
# Caching — /status must not spend tokens on every hit
# ---------------------------------------------------------------------------

def test_results_are_cached(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    calls = []

    monkeypatch.setattr(ph, "_PROBES", dict(ph._PROBES))
    ph._PROBES["openai"] = lambda: (calls.append(1), ph._result(True))[1]

    first = ph.check_provider("openai", force=True)
    second = ph.check_provider("openai")

    assert len(calls) == 1
    assert first["cached"] is False
    assert second["cached"] is True


def test_force_bypasses_the_cache(monkeypatch):
    calls = []
    monkeypatch.setattr(ph, "_PROBES", dict(ph._PROBES))
    ph._PROBES["openai"] = lambda: (calls.append(1), ph._result(True))[1]

    ph.check_provider("openai", force=True)
    ph.check_provider("openai", force=True)
    assert len(calls) == 2


# ---------------------------------------------------------------------------
# Overall verdict
# ---------------------------------------------------------------------------

def _all_probes(monkeypatch, openai_ok=True, anthropic_ok=True, google_ok=True):
    monkeypatch.setattr(ph, "_PROBES", {
        "openai": lambda: ph._result(openai_ok, error_class=None if openai_ok
                                     else "insufficient_quota"),
        "anthropic": lambda: ph._result(anthropic_ok),
        "google": lambda: ph._result(google_ok),
    })
    monkeypatch.setattr(ph, "_ROLES", {
        "openai": ("QC fact-checker", True),
        "anthropic": ("Researcher", True),
        "google": ("weekly Auditor", False),
    })


def test_all_healthy_is_ok(monkeypatch):
    _all_probes(monkeypatch)
    monkeypatch.setattr("ledger.db.get_qc_error_streak", lambda: 0)
    out = ph.check_all_providers(force=True)
    assert out["ok"] is True
    assert out["degraded_by"] == []


def test_openai_down_degrades_the_system(monkeypatch):
    """QC is a hard gate — nothing ships without it."""
    _all_probes(monkeypatch, openai_ok=False)
    monkeypatch.setattr("ledger.db.get_qc_error_streak", lambda: 0)
    out = ph.check_all_providers(force=True)
    assert out["ok"] is False
    assert "openai" in out["degraded_by"]


def test_anthropic_down_degrades_the_system(monkeypatch):
    _all_probes(monkeypatch, anthropic_ok=False)
    monkeypatch.setattr("ledger.db.get_qc_error_streak", lambda: 0)
    out = ph.check_all_providers(force=True)
    assert out["ok"] is False
    assert "anthropic" in out["degraded_by"]


def test_google_down_is_reported_but_does_not_degrade_trading(monkeypatch):
    """Gemini only powers the weekly Auditor. Losing it costs analysis, not trades."""
    _all_probes(monkeypatch, google_ok=False)
    monkeypatch.setattr("ledger.db.get_qc_error_streak", lambda: 0)
    out = ph.check_all_providers(force=True)
    assert out["ok"] is True
    assert out["degraded_by"] == []
    assert out["providers"]["google"]["ok"] is False


def test_a_live_qc_error_streak_degrades_even_when_probes_pass(monkeypatch):
    """
    A synthetic probe can succeed while the real pipeline is failing. The
    persisted streak is what actually happened, so it wins.
    """
    _all_probes(monkeypatch)
    monkeypatch.setattr("ledger.db.get_qc_error_streak", lambda: 4)
    out = ph.check_all_providers(force=True)
    assert out["ok"] is False
    assert out["qc_consecutive_errors"] == 4
