"""
One-time Kite authentication setup — run this once, then it's automatic forever.

    python setup/setup_kite_auth.py

What this does:
1. Opens the Zerodha TOTP settings page in your browser
2. Walks you through finding your TOTP secret step by step
3. Tests all three credentials (user ID, password, TOTP secret) BEFORE saving
4. Saves working credentials to .env
5. Prints the exact values to copy into Railway

After this script succeeds, the system will refresh your Kite token
automatically at 7:30 AM IST every weekday — no further action needed.
"""

import sys
import os
import re
import webbrowser
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv, set_key
load_dotenv()

ENV_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def section(title: str):
    print(f"\n{'─' * 60}")
    print(f"  {title}")
    print('─' * 60)


def ask(prompt: str, secret: bool = False) -> str:
    """Prompt the user, strip whitespace. Re-ask if empty."""
    import getpass
    while True:
        value = (getpass.getpass(prompt) if secret else input(prompt)).strip()
        if value:
            return value
        print("  ↳ Can't be empty. Try again.")


def ok(msg: str):
    print(f"  ✓ {msg}")


def warn(msg: str):
    print(f"  ⚠  {msg}")


def fail(msg: str):
    print(f"\n  ✗ {msg}")
    sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# Pre-flight: check pyotp and requests are installed
# ─────────────────────────────────────────────────────────────────────────────

def check_dependencies():
    missing = []
    for pkg in ("pyotp", "requests", "kiteconnect"):
        try:
            __import__(pkg.replace("-", "_").replace("kiteconnect", "kiteconnect"))
        except ImportError:
            missing.append(pkg)
    if missing:
        print(f"\nMissing packages: {', '.join(missing)}")
        print("Run this first:")
        print(f"  pip install {' '.join(missing)}")
        sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# Step 1: Collect Zerodha User ID
# ─────────────────────────────────────────────────────────────────────────────

def get_user_id() -> str:
    section("STEP 1 of 3 — Your Zerodha User ID")
    print("""
  This is the 6-character ID Zerodha sent when you created your account.
  It looks like: AB1234 or ZY9876
  You can find it at: kite.zerodha.com → top-right menu → Profile
    """)
    existing = os.getenv("KITE_USER_ID", "")
    if existing:
        print(f"  Found existing value: {existing}")
        use = ask("  Use this? (y/n): ").lower()
        if use == "y":
            return existing

    user_id = ask("  Enter your Zerodha User ID: ").upper()
    if not re.match(r"^[A-Z]{2}\d{4}$", user_id):
        warn(f"'{user_id}' doesn't look like a standard Zerodha ID (XX1234).")
        confirm = ask("  Continue anyway? (y/n): ").lower()
        if confirm != "y":
            return get_user_id()
    ok(f"User ID: {user_id}")
    return user_id


# ─────────────────────────────────────────────────────────────────────────────
# Step 2: Collect Password
# ─────────────────────────────────────────────────────────────────────────────

def get_password() -> str:
    section("STEP 2 of 3 — Your Zerodha Login Password")
    print("""
  This is the password you use to log into kite.zerodha.com.
  (Not your Kite Connect API secret — that's different.)

  It will NOT be displayed as you type.
    """)
    password = ask("  Enter your Zerodha password: ", secret=True)
    ok("Password received (hidden).")
    return password


# ─────────────────────────────────────────────────────────────────────────────
# Step 3: Collect TOTP Secret
# ─────────────────────────────────────────────────────────────────────────────

def get_totp_secret() -> str:
    section("STEP 3 of 3 — Your TOTP Secret Key")
    print("""
  This is the secret key behind your authenticator app's 6-digit codes.
  It is NOT the 6-digit code itself — it's the fixed key used to generate them.

  HOW TO FIND IT:
  ┌─────────────────────────────────────────────────────────┐
  │  1. We'll open Zerodha security settings in your browser│
  │  2. Go to: My Profile → Security → 2FA Authentication  │
  │  3. Click "Disable TOTP" (don't worry — we'll re-enable)│
  │  4. Then click "Enable TOTP"                            │
  │  5. A QR code appears. Below it: "Can't scan the code?" │
  │  6. Click that link → a ~32-character key appears        │
  │  7. Copy that key and paste it here                     │
  │  8. Then scan the same QR code with your auth app       │
  └─────────────────────────────────────────────────────────┘

  IMPORTANT: After pasting the key here, you MUST also scan the QR
  code with your authenticator app (Google Authenticator, Authy, etc.)
  to keep getting your 6-digit codes. Don't close the Zerodha page
  until you've done both.
    """)

    input("  Press Enter to open Zerodha security settings in your browser...")
    webbrowser.open("https://console.zerodha.com/account/security")
    print("\n  Browser opened. Follow the steps above, then come back here.")
    print("  (The key looks like: JBSWY3DPEHPK3PXP — uppercase letters and numbers)")
    print()

    while True:
        secret = ask("  Paste your TOTP secret key here: ").upper().replace(" ", "")

        # Validate format
        if not re.match(r"^[A-Z2-7]{16,64}$", secret):
            warn("That doesn't look like a valid base32 TOTP key.")
            warn("It should be 16-64 characters, using only A-Z and 2-7.")
            retry = ask("  Try again? (y/n): ").lower()
            if retry != "y":
                fail("Cannot continue without a valid TOTP secret.")
            continue

        # Validate it generates a working 6-digit code
        try:
            import pyotp
            code = pyotp.TOTP(secret).now()
            if not code.isdigit() or len(code) != 6:
                raise ValueError("Invalid code generated")
            ok(f"TOTP secret is valid (current code: {code})")
            print()
            print("  ⚠  Did you also scan the QR code with your authenticator app?")
            scanned = ask("  Yes, I've updated my authenticator app (y/n): ").lower()
            if scanned != "y":
                print("\n  Please scan the QR code on the Zerodha page with your auth app first.")
                print("  The QR code and the secret key shown below it are two ways of adding")
                print("  the same account — you need the app to keep getting your 6-digit codes.")
                input("  Press Enter when done...")
            break
        except Exception as e:
            warn(f"Could not validate TOTP secret: {e}")
            retry = ask("  Try a different key? (y/n): ").lower()
            if retry != "y":
                fail("Cannot continue without a valid TOTP secret.")

    return secret


# ─────────────────────────────────────────────────────────────────────────────
# Live test: attempt a real token refresh before saving anything
# ─────────────────────────────────────────────────────────────────────────────

def test_credentials(user_id: str, password: str, totp_secret: str) -> str:
    section("TESTING — verifying credentials against Zerodha...")
    print("  Making a real login attempt now. This may take 5-10 seconds.\n")

    api_key    = os.getenv("KITE_API_KEY")
    api_secret = os.getenv("KITE_API_SECRET")
    if not api_key or not api_secret:
        fail("KITE_API_KEY and KITE_API_SECRET must be in .env before running this script.")

    # Temporarily set env vars so auto_refresh_token() can use them
    os.environ["KITE_USER_ID"]    = user_id
    os.environ["KITE_PASSWORD"]   = password
    os.environ["KITE_TOTP_SECRET"] = totp_secret

    try:
        from setup.auto_refresh_kite_token import auto_refresh_token
        access_token = auto_refresh_token()
        ok(f"Login successful! Token: {access_token[:8]}...")
        return access_token
    except RuntimeError as e:
        # Clear env vars so they're not accidentally used in a broken state
        for v in ("KITE_USER_ID", "KITE_PASSWORD", "KITE_TOTP_SECRET"):
            os.environ.pop(v, None)

        print(f"\n  ✗ Login failed: {e}\n")
        print("  Common causes:")
        print("  • Wrong password (try logging into kite.zerodha.com manually)")
        print("  • Wrong TOTP secret (the key, not the 6-digit code)")
        print("  • KITE_API_KEY doesn't match the app in kite.trade developer console")
        print()
        retry = ask("  Try again from the beginning? (y/n): ").lower()
        if retry == "y":
            main()
            sys.exit(0)
        fail("Setup aborted.")


# ─────────────────────────────────────────────────────────────────────────────
# Save to .env + print Railway instructions
# ─────────────────────────────────────────────────────────────────────────────

def save_and_print_instructions(user_id: str, password: str, totp_secret: str):
    section("SAVING — writing to .env")

    set_key(ENV_FILE, "KITE_USER_ID",     user_id)
    set_key(ENV_FILE, "KITE_PASSWORD",    password)
    set_key(ENV_FILE, "KITE_TOTP_SECRET", totp_secret)
    ok("Credentials saved to .env")

    section("LAST STEP — Add these 3 variables to Railway")
    print("""
  Go to: railway.app → your project → AI Trading System service → Variables

  Click "New Variable" and add each of these:

  ┌────────────────────────┬──────────────────────────────────────┐
  │ Variable name          │ Value                                │
  ├────────────────────────┼──────────────────────────────────────┤""")
    print(f"  │ KITE_USER_ID           │ {user_id:<36} │")
    print(f"  │ KITE_PASSWORD          │ {'(your password — type it in)' :<36} │")
    print(f"  │ KITE_TOTP_SECRET       │ {totp_secret:<36} │")
    print("""  └────────────────────────┴──────────────────────────────────────┘

  After adding all three, Railway will redeploy automatically.
  The system will then refresh your Kite token at 7:30 AM IST every weekday.

  You're done. No more manual steps needed for the Kite token.
    """)


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    print("\n" + "═" * 60)
    print("  Kite Authentication Setup")
    print("  Takes about 3 minutes. Do this once, then it's automatic.")
    print("═" * 60)

    check_dependencies()

    user_id     = get_user_id()
    password    = get_password()
    totp_secret = get_totp_secret()

    test_credentials(user_id, password, totp_secret)
    save_and_print_instructions(user_id, password, totp_secret)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nSetup cancelled.")
        sys.exit(0)
