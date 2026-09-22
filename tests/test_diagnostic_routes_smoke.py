"""
Smoke tests for the diagnostic GET routes (/status, /diagnose_cycle,
/cycle_history, /signal_history, /diagnose_universe) — a route smoke test
TODOS.md had flagged as missing ("Add a route smoke test that hits every
endpoint with a valid secret and asserts non-500, so a refactor can never
silently break a route again").

2026-09-22: this exact gap let /diagnose_cycle regress to a hard 500 for at
least 3 days undetected — it imported STRATEGY_BASKETS from the wrong module
(researcher.regime_classifier instead of researcher.signal_generator), a
bug the outer except caught and turned into a generic 500 page. Nothing
caught it because nothing exercised the route at all.

These do NOT attempt to fully exercise the happy path (that needs live Kite/
LLM credentials, out of scope for a unit test) — they only assert that with
a valid secret, and no Kite token configured, each route fails CLEANLY
(its own documented "no token" / "no data" response) rather than 500ing on
a broken import or an undefined name. That is enough to catch the class of
bug that actually happened here: the broken import raises before Kite is
ever touched, so it fails the same way with or without real credentials.
"""

import os
import sqlite3
import tempfile

import pytest


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("APPROVAL_SECRET", "route-smoke-test-secret")

    # /status calls monitor.provider_health.check_all_providers(), which is
    # a REAL reachability probe against Anthropic/OpenAI/Google — by design
    # (its own docstring: "the check that would have caught the QC outage").
    # That is exactly right for a human hitting /status, and exactly wrong
    # for a unit test: it is slow, needs live network + API keys, and would
    # burn real quota on every test run. Stub it here.
    import monitor.provider_health as ph

    monkeypatch.setattr(
        ph, "check_all_providers",
        lambda force=False: {
            "ok": True, "degraded_by": [], "qc_consecutive_errors": 0,
            "providers": {},
        },
    )

    import webhook_server as ws

    ws.app.config["TESTING"] = True
    with ws.app.test_client() as c:
        yield c


@pytest.fixture()
def empty_ledger(monkeypatch):
    """A real, empty ledger DB — enough for signal_history/cycle_history to
    return their documented "no data" response instead of a DB error."""
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


DIAGNOSTIC_GET_ROUTES = [
    "/status",
    "/diagnose_cycle",
    "/cycle_history",
    "/signal_history",
    "/diagnose_universe",
]

# /diagnose_cycle and /diagnose_universe both import kiteconnect.KiteConnect
# as their very first line. On some local dev machines that transitively
# pulls in twisted -> pyOpenSSL, and a mismatched system-wide pyOpenSSL/
# cryptography install there raises AttributeError deep inside OpenSSL.crypto
# (`module 'lib' has no attribute 'GEN_EMAIL'`) — a local environment
# problem with zero relation to this codebase (confirmed by reproducing it
# with a bare `from kiteconnect import KiteConnect` outside any of our
# code). Railway's clean container does not have this conflict. Skip rather
# than fail so this suite stays a reliable signal on machines with a broken
# local OpenSSL, without masking a real regression anywhere else.
try:
    from kiteconnect import KiteConnect as _KiteConnectProbe  # noqa: F401
    _KITECONNECT_IMPORTABLE = True
except Exception as _e:  # pragma: no cover - environment-dependent
    _KITECONNECT_IMPORTABLE = False
    _KITECONNECT_IMPORT_ERROR = _e

_KITECONNECT_ROUTES = {"/diagnose_cycle", "/diagnose_universe"}


@pytest.mark.parametrize("route", DIAGNOSTIC_GET_ROUTES)
def test_wrong_secret_is_refused_not_500(client, route):
    resp = client.get(f"{route}?secret=definitely-wrong")
    assert resp.status_code == 403, f"{route}: expected 403, got {resp.status_code}"


@pytest.mark.parametrize("route", DIAGNOSTIC_GET_ROUTES)
def test_valid_secret_fails_clean_not_500(client, empty_ledger, monkeypatch, route):
    """
    No Kite token, no LLM keys configured. Every one of these routes is
    documented to degrade gracefully (a "no token" 503, a "no data" 200, or
    a checks dict marking components unhealthy) rather than crash. A 500
    here means something in the route body — most often an import or a name
    that no longer exists — is broken independently of any live credential.
    """
    if route in _KITECONNECT_ROUTES and not _KITECONNECT_IMPORTABLE:
        pytest.skip(
            f"kiteconnect is not importable in this environment "
            f"({_KITECONNECT_IMPORT_ERROR!r}) — a local dependency conflict, "
            f"not a code issue. See the module docstring above."
        )

    from ledger.db import get_kite_token

    assert get_kite_token() is None  # fresh DB — sanity check on the fixture itself

    resp = client.get(f"{route}?secret=route-smoke-test-secret")
    assert resp.status_code != 500, (
        f"{route}: got a 500 with no live credentials configured — likely a "
        f"broken import or undefined name in the route body. Response: "
        f"{resp.get_data(as_text=True)[:2000]}"
    )
