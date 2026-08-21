"""
Diagnostic endpoint auth.

Six endpoints compared `secret != expected` — a non-constant-time compare —
against APPROVAL_SECRET, the same key that signs approve/reject HMACs. The
approval path itself uses hmac.compare_digest, so the weakest comparison in
the system guarded the strongest capability: a timing oracle on /status would
have been a forgery oracle on /email_action.

The secret also travels in the query string, so it lands in Railway access
logs and browser history — which makes sharing one key between "read my
diagnostics" and "approve my trades" the more pressing half of the problem.
"""

import pytest

import webhook_server as ws


@pytest.fixture()
def client():
    ws.app.config["TESTING"] = True
    with ws.app.test_client() as c:
        yield c


DIAGNOSTIC_ROUTES = [
    "/status",
    "/send_test_email",
    "/diagnose_cycle",
    "/cycle_history",
    "/diagnose_universe",
    "/refresh_universe",
]


# ---------------------------------------------------------------------------
# The check itself
# ---------------------------------------------------------------------------

def test_uses_constant_time_comparison():
    """
    Asserted structurally: the module must call hmac.compare_digest and must
    not have regressed to a plain !=.
    """
    import inspect

    src = inspect.getsource(ws._check_diagnostic_secret)
    assert "compare_digest" in src
    assert "!=" not in src


def test_correct_secret_passes(monkeypatch):
    monkeypatch.setenv("DIAGNOSTIC_SECRET", "diag-secret")
    assert ws._check_diagnostic_secret("diag-secret") is True


@pytest.mark.parametrize("wrong", ["", "nope", "diag-secre", "diag-secret ", "DIAG-SECRET"])
def test_wrong_secret_fails(monkeypatch, wrong):
    monkeypatch.setenv("DIAGNOSTIC_SECRET", "diag-secret")
    assert ws._check_diagnostic_secret(wrong) is False


def test_fails_closed_when_nothing_is_configured(monkeypatch):
    """No secret must mean no access, not open access."""
    monkeypatch.delenv("DIAGNOSTIC_SECRET", raising=False)
    monkeypatch.delenv("APPROVAL_SECRET", raising=False)
    assert ws._check_diagnostic_secret("") is False
    assert ws._check_diagnostic_secret("anything") is False


# ---------------------------------------------------------------------------
# Key separation
# ---------------------------------------------------------------------------

def test_dedicated_secret_is_preferred(monkeypatch):
    monkeypatch.setenv("APPROVAL_SECRET", "approval-key")
    monkeypatch.setenv("DIAGNOSTIC_SECRET", "diag-key")

    assert ws._diagnostic_secret() == ("diag-key", True)
    assert ws._check_diagnostic_secret("diag-key") is True
    # the approval key must NOT open diagnostics once they are separated
    assert ws._check_diagnostic_secret("approval-key") is False


def test_falls_back_to_approval_secret_for_continuity(monkeypatch):
    """
    Existing bookmarks keep working through the deploy. Reported as
    non-dedicated so it is visible rather than assumed.
    """
    monkeypatch.setenv("APPROVAL_SECRET", "approval-key")
    monkeypatch.delenv("DIAGNOSTIC_SECRET", raising=False)

    assert ws._diagnostic_secret() == ("approval-key", False)
    assert ws._check_diagnostic_secret("approval-key") is True


def test_leaking_the_diagnostic_key_does_not_grant_trade_approval(monkeypatch):
    """
    The point of the split. The diagnostic secret rides in URLs and therefore
    in logs; it must not be usable to sign an approve link.
    """
    monkeypatch.setenv("APPROVAL_SECRET", "approval-key")
    monkeypatch.setenv("DIAGNOSTIC_SECRET", "diag-key")

    import importlib

    import alerts.gmail_alert as ga
    importlib.reload(ga)
    try:
        real = ga._make_token("approve", 1)
        assert ga.verify_token("approve", 1, real) is True

        import hashlib
        import hmac as _hmac

        forged = _hmac.new(
            b"diag-key", b"approve:1", hashlib.sha256
        ).hexdigest()
        assert ga.verify_token("approve", 1, forged) is False
    finally:
        importlib.reload(ga)


# ---------------------------------------------------------------------------
# The routes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("route", DIAGNOSTIC_ROUTES)
def test_route_rejects_a_missing_secret(client, monkeypatch, route):
    monkeypatch.setenv("DIAGNOSTIC_SECRET", "diag-secret")
    assert client.get(route).status_code == 403


@pytest.mark.parametrize("route", DIAGNOSTIC_ROUTES)
def test_route_rejects_a_wrong_secret(client, monkeypatch, route):
    monkeypatch.setenv("DIAGNOSTIC_SECRET", "diag-secret")
    assert client.get(f"{route}?secret=wrong").status_code == 403


@pytest.mark.parametrize("route", DIAGNOSTIC_ROUTES)
def test_route_rejects_the_approval_key_once_separated(
    client, monkeypatch, route
):
    monkeypatch.setenv("APPROVAL_SECRET", "approval-key")
    monkeypatch.setenv("DIAGNOSTIC_SECRET", "diag-secret")
    assert client.get(f"{route}?secret=approval-key").status_code == 403


@pytest.mark.parametrize("route", DIAGNOSTIC_ROUTES)
def test_route_is_closed_when_no_secret_is_configured(
    client, monkeypatch, route
):
    monkeypatch.delenv("DIAGNOSTIC_SECRET", raising=False)
    monkeypatch.delenv("APPROVAL_SECRET", raising=False)
    assert client.get(f"{route}?secret=").status_code == 403


def test_a_403_never_echoes_the_expected_secret(client, monkeypatch):
    monkeypatch.setenv("DIAGNOSTIC_SECRET", "super-secret-value")
    body = client.get("/status?secret=wrong").get_data(as_text=True)
    assert "super-secret-value" not in body


def test_health_endpoints_stay_open(client):
    """/ and /health are liveness probes and must not require a secret."""
    assert client.get("/health").status_code == 200
    assert client.get("/").status_code == 200
