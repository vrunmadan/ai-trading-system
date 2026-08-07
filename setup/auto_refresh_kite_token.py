"""
Automated Kite Connect token refresh using TOTP — no browser, no manual steps.

Requires these env vars (set in Railway env vars + local .env):
  KITE_USER_ID       - Your Zerodha user ID (e.g. AB1234)
  KITE_PASSWORD      - Your Zerodha login password
  KITE_TOTP_SECRET   - Base32 TOTP secret from your authenticator app
  KITE_API_KEY       - From kite.trade developer console
  KITE_API_SECRET    - From kite.trade developer console

How to find your TOTP secret:
  When you set up Zerodha TOTP (the QR code step in Zerodha's 2FA setup),
  there is a link below the QR code that says "Can't scan? Use this key instead."
  That ~32-character string is your TOTP_SECRET. If you can't find it, disable
  TOTP in Zerodha account settings and re-enable it — note the key this time.

Called automatically by the scheduler at 7:30 AM IST on weekdays.
Also runs at startup (in run_server.py) to ensure a fresh token before 9:15.

Manual run:  python setup/auto_refresh_kite_token.py
"""

import sys
import os
import re
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv()

log = logging.getLogger(__name__)

_REQUIRED_VARS = [
    "KITE_USER_ID", "KITE_PASSWORD", "KITE_TOTP_SECRET",
    "KITE_API_KEY", "KITE_API_SECRET",
]


def auto_refresh_token() -> str:
    """
    Full TOTP-based Kite login flow. Returns a fresh access_token string.
    Saves token to the ledger DB + updates os.environ for the current process.
    Raises RuntimeError with a clear message on any failure.
    """
    # Guard: check all required vars up front
    missing = [v for v in _REQUIRED_VARS if not os.getenv(v)]
    if missing:
        raise RuntimeError(
            f"Missing env vars: {', '.join(missing)}\n"
            "Set these in Railway → Variables (not in chat, not in git)."
        )

    user_id    = os.environ["KITE_USER_ID"]
    password   = os.environ["KITE_PASSWORD"]
    totp_secret = os.environ["KITE_TOTP_SECRET"]
    api_key    = os.environ["KITE_API_KEY"]
    api_secret = os.environ["KITE_API_SECRET"]

    try:
        import requests
    except ImportError:
        raise RuntimeError("requests not installed. Run: pip install requests")

    try:
        import pyotp
    except ImportError:
        raise RuntimeError("pyotp not installed. Run: pip install pyotp")

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    # ----------------------------------------------------------------
    # Step 1: Password login
    # ----------------------------------------------------------------
    log.info("Kite token refresh: Step 1 — password login...")
    try:
        resp = session.post(
            "https://kite.zerodha.com/api/login",
            data={"user_id": user_id, "password": password},
            timeout=20,
        )
        resp.raise_for_status()
    except Exception as e:
        raise RuntimeError(f"Password login request failed: {e}")

    login_data = resp.json()
    if login_data.get("status") != "success":
        raise RuntimeError(
            f"Password login rejected by Zerodha: {login_data.get('message', login_data)}"
        )
    request_id = login_data["data"]["request_id"]

    # ----------------------------------------------------------------
    # Step 2: TOTP
    # ----------------------------------------------------------------
    log.info("Step 2 — TOTP verification...")
    try:
        totp_code = pyotp.TOTP(totp_secret).now()
        resp = session.post(
            "https://kite.zerodha.com/api/twofa",
            data={
                "user_id": user_id,
                "request_id": request_id,
                "twofa_value": totp_code,
                "twofa_type": "totp",
            },
            timeout=20,
        )
        resp.raise_for_status()
    except Exception as e:
        raise RuntimeError(f"TOTP request failed: {e}")

    twofa_data = resp.json()
    if twofa_data.get("status") != "success":
        raise RuntimeError(
            f"TOTP rejected by Zerodha: {twofa_data.get('message', twofa_data)}\n"
            "If your TOTP code is wrong, double-check KITE_TOTP_SECRET."
        )

    # ----------------------------------------------------------------
    # Step 3: Grab request_token from API login redirect
    # ----------------------------------------------------------------
    log.info("Step 3 — fetching request_token via API redirect...")
    try:
        resp = session.get(
            f"https://kite.zerodha.com/connect/login?v=3&api_key={api_key}",
            allow_redirects=False,
            timeout=20,
        )
    except Exception as e:
        raise RuntimeError(f"API redirect request failed: {e}")

    redirect_url = resp.headers.get("Location", "")
    match = re.search(r"request_token=([A-Za-z0-9]+)", redirect_url)
    if not match:
        raise RuntimeError(
            f"request_token not found in redirect URL: {redirect_url!r}\n"
            "Possible causes: KITE_API_KEY mismatch, app suspended, or Zerodha changed login flow."
        )
    request_token = match.group(1)

    # ----------------------------------------------------------------
    # Step 4: Exchange request_token for access_token
    # ----------------------------------------------------------------
    log.info("Step 4 — generating access_token...")
    try:
        from kiteconnect import KiteConnect
        kite = KiteConnect(api_key=api_key)
        session_data = kite.generate_session(request_token, api_secret=api_secret)
        access_token = session_data["access_token"]
    except Exception as e:
        raise RuntimeError(f"generate_session failed: {e}")

    # ----------------------------------------------------------------
    # Step 5: Persist token so get_kite_client() picks it up immediately
    # ----------------------------------------------------------------
    from ledger.db import save_kite_token
    save_kite_token(access_token)

    # Also set in current process env so existing in-memory code sees it
    os.environ["KITE_ACCESS_TOKEN"] = access_token

    log.info(f"Kite token refreshed successfully. ({access_token[:8]}...) Valid until 6 AM IST.")
    return access_token


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        token = auto_refresh_token()
        print(f"\nDone. Token (first 8 chars): {token[:8]}...")
        print("Token saved to DB. The scheduler will auto-refresh this daily at 7:30 AM IST.")
    except RuntimeError as e:
        print(f"\nFailed: {e}")
        sys.exit(1)
