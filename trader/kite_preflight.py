"""
Kite auth preflight — runs before every research cycle.

Failure this defends against (2026-09-30): the Kite access token was invalid, so
every cycle died quietly inside generate_signal() (kite.instruments() failed ->
"No instruments loaded -> aborting cycle"), logged nothing to cycle_log, and the
first anyone heard of it was the 15:35 summary saying "no candidate cleared 65%".

The preflight makes the auth failure loud at the FIRST cycle (09:15 IST):
  * one cheap authenticated call (kite.profile()) before the cycle runs
  * on an auth failure: skip the cycle, count it in kv_store, and email the login
    link on the 1st failure of the day and again on the 4th (12:15 reminder)
  * anything that is NOT clearly an auth failure (network blip, 5xx) does not
    block the cycle -- the cycle's own error handling deals with it.
"""

import json
import logging
from datetime import datetime

import pytz

log = logging.getLogger(__name__)
IST = pytz.timezone("Asia/Kolkata")

ALERT_AT_FAILURE_COUNTS = (1, 4)   # 09:15 first alert, 12:15 reminder


def _today() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d")


def _key(day: str) -> str:
    return f"kite_auth_failures_{day}"


def is_auth_error(exc: BaseException) -> bool:
    """True only for errors that mean 'the Kite login is invalid/missing'."""
    try:
        from kiteconnect.exceptions import TokenException
        if isinstance(exc, TokenException):
            return True
    except Exception:
        pass
    if isinstance(exc, EnvironmentError) and "access_token" in str(exc).lower():
        return True
    msg = str(exc).lower()
    return "access_token" in msg or "incorrect `api_key`" in msg or "tokenexception" in msg


def check_kite_auth() -> tuple[bool, str]:
    """(ok, reason). ok=False ONLY on a definite auth failure."""
    try:
        from trader.kite_client import get_kite_client
        get_kite_client().profile()
        return True, "ok"
    except Exception as e:
        if is_auth_error(e):
            return False, str(e)[:300]
        log.warning(f"Kite preflight inconclusive (not an auth error), letting cycle run: {e}")
        return True, f"inconclusive: {e}"


def record_auth_failure(reason: str, day: str | None = None) -> int:
    """Increment today's auth-failure counter; returns the new count."""
    from ledger.db import kv_get, kv_set
    day = day or _today()
    count = 0
    cur = kv_get(_key(day))
    if cur:
        try:
            count = int(json.loads(cur[0]).get("count", 0))
        except Exception:
            count = 0
    count += 1
    kv_set(_key(day), json.dumps({"count": count, "reason": reason[:300]}))
    return count


def get_auth_failures(day: str | None = None) -> tuple[int, str]:
    """(count, last_reason) for the day; (0, '') if none. Never raises."""
    try:
        from ledger.db import kv_get
        cur = kv_get(_key(day or _today()))
        if not cur:
            return 0, ""
        d = json.loads(cur[0])
        return int(d.get("count", 0)), str(d.get("reason", ""))
    except Exception:
        return 0, ""


def preflight_ok_or_alert() -> bool:
    """
    True -> run the cycle. False -> auth is broken; the cycle was skipped and
    (when due) the alert email was sent. Never raises.
    """
    try:
        ok, reason = check_kite_auth()
        if ok:
            return True
        count = record_auth_failure(reason)
        log.error(f"Kite auth preflight FAILED (#{count} today): {reason}")
        if count in ALERT_AT_FAILURE_COUNTS:
            from alerts.gmail_alert import send_kite_auth_failed_alert
            send_kite_auth_failed_alert(reason, count)
        return False
    except Exception as e:
        # The guard itself must never take the cycle down.
        log.error(f"Kite preflight guard error (running cycle anyway): {e}", exc_info=True)
        return True
