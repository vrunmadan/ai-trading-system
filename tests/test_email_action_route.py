"""
Route-level tests for /email_action (2026-09-19 review, item 8).

Two things had no test coverage at all before this fix: the GET-mutates-
state bug (Gmail's own image/link-scanning proxy, and corporate "Safe
Links"-style scanners, fetch every URL in an email before a human ever
clicks it — a GET that itself called handle_email_action() meant those
automated prefetches could silently approve or reject real trades with
nobody at the keyboard) and token expiry (a leaked, forwarded, or simply
forgotten link worked forever). Both are fixed by moving the actual side
effect behind POST and folding an expiry timestamp into the signed token.
These tests pin that behavior at the Flask route itself, not just at the
alerts.gmail_alert functions the route calls.
"""

import time

import pytest


@pytest.fixture()
def client():
    import webhook_server as ws
    ws.app.config["TESTING"] = True
    with ws.app.test_client() as c:
        yield c


@pytest.fixture()
def approval_env(monkeypatch):
    monkeypatch.setenv("APPROVAL_SECRET", "route-test-secret")
    import importlib
    import alerts.gmail_alert as ga
    importlib.reload(ga)
    yield ga
    importlib.reload(ga)


def _link(ga, action="approve", signal_id=42, ttl_seconds=3600):
    expires_at = int(time.time()) + ttl_seconds
    token = ga._make_token(action, signal_id, expires_at)
    return action, signal_id, token, expires_at


# ---------------------------------------------------------------------------
# GET must render a confirmation page and never execute anything
# ---------------------------------------------------------------------------

def test_get_renders_a_confirmation_page_without_acting(client, approval_env, monkeypatch):
    ga = approval_env
    calls = []
    monkeypatch.setattr(
        ga, "handle_email_action",
        lambda action, sid: calls.append((action, sid)) or ("x", True, None),
    )

    action, signal_id, token, exp = _link(ga)
    resp = client.get(f"/email_action?action={action}&id={signal_id}&token={token}&exp={exp}")

    assert resp.status_code == 200
    assert calls == [], "a bare GET must not call handle_email_action"
    body = resp.get_data(as_text=True)
    assert "<form" in body and 'method="POST"' in body
    assert f'value="{token}"' in body
    assert f'value="{exp}"' in body


def test_post_is_what_actually_executes(client, approval_env, monkeypatch):
    ga = approval_env
    calls = []

    def _fake_handle(action, sid):
        calls.append((action, sid))
        return ("done", True, None)

    monkeypatch.setattr(ga, "handle_email_action", _fake_handle)

    action, signal_id, token, exp = _link(ga)
    resp = client.post(
        "/email_action",
        data={"action": action, "id": str(signal_id), "token": token, "exp": str(exp)},
    )

    assert calls == [(action, signal_id)]
    assert resp.status_code == 200


def test_post_with_a_reused_get_confirmation_page_still_only_acts_once_per_submit(
    client, approval_env, monkeypatch
):
    """The confirmation page's own form is the only thing that can trigger
    a POST from a normal tap; simulate the two-step flow end to end."""
    ga = approval_env
    calls = []
    monkeypatch.setattr(
        ga, "handle_email_action",
        lambda action, sid: calls.append((action, sid)) or ("done", True, None),
    )

    action, signal_id, token, exp = _link(ga, action="reject")
    get_resp = client.get(f"/email_action?action={action}&id={signal_id}&token={token}&exp={exp}")
    assert get_resp.status_code == 200
    assert calls == []

    post_resp = client.post(
        "/email_action",
        data={"action": action, "id": str(signal_id), "token": token, "exp": str(exp)},
    )
    assert post_resp.status_code == 200
    assert calls == [(action, signal_id)]


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------

def test_expired_link_is_refused_on_get(client, approval_env):
    ga = approval_env
    action, signal_id, token, exp = _link(ga, ttl_seconds=-10)  # already in the past
    resp = client.get(f"/email_action?action={action}&id={signal_id}&token={token}&exp={exp}")
    assert resp.status_code == 403
    assert "expired" in resp.get_data(as_text=True).lower()


def test_expired_link_is_also_refused_on_post(client, approval_env, monkeypatch):
    ga = approval_env
    calls = []
    monkeypatch.setattr(
        ga, "handle_email_action",
        lambda action, sid: calls.append((action, sid)) or ("x", True, None),
    )
    action, signal_id, token, exp = _link(ga, ttl_seconds=-10)
    resp = client.post(
        "/email_action",
        data={"action": action, "id": str(signal_id), "token": token, "exp": str(exp)},
    )
    assert resp.status_code == 403
    assert calls == []


def test_extending_exp_without_the_secret_is_rejected(client, approval_env):
    """
    exp is part of the signed message, not a separate check — a link
    recipient editing exp= in the URL to buy more time invalidates the
    signature instead of extending it.
    """
    ga = approval_env
    action, signal_id, token, exp = _link(ga)
    tampered_exp = exp + 10_000_000
    resp = client.get(
        f"/email_action?action={action}&id={signal_id}&token={token}&exp={tampered_exp}"
    )
    assert resp.status_code == 403


def test_missing_exp_param_is_rejected(client, approval_env):
    ga = approval_env
    action, signal_id, token, exp = _link(ga)
    resp = client.get(f"/email_action?action={action}&id={signal_id}&token={token}")
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Token validity, still enforced at the route
# ---------------------------------------------------------------------------

def test_post_with_invalid_token_is_refused(client, approval_env, monkeypatch):
    ga = approval_env
    calls = []
    monkeypatch.setattr(
        ga, "handle_email_action",
        lambda action, sid: calls.append((action, sid)) or ("x", True, None),
    )
    action, signal_id, _token, exp = _link(ga)
    resp = client.post(
        "/email_action",
        data={"action": action, "id": str(signal_id), "token": "0" * 64, "exp": str(exp)},
    )
    assert resp.status_code == 403
    assert calls == []
