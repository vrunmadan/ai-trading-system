"""
Daily Kite Connect token refresh.

Run this each morning before 9:15 AM IST — Zerodha access tokens
expire at 6 AM every day and must be refreshed before the market opens.

    python setup/refresh_kite_token.py

What this does:
1. Opens the Kite login URL in your browser
2. You log in normally (username + password + TOTP if enabled)
3. Zerodha redirects to localhost:8080 — copy the `request_token`
   from the URL and paste it here
4. Script exchanges it for a fresh access_token and writes it to .env

You only need to do this once per trading day. Set a phone alarm for
9:00 AM on weekdays as a reminder.
"""

import sys
import os
import hashlib
import webbrowser
import re

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv, set_key
load_dotenv()

try:
    from kiteconnect import KiteConnect
except ImportError:
    print("ERROR: kiteconnect not installed. Run: pip install kiteconnect")
    sys.exit(1)

ENV_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")

API_KEY = os.getenv("KITE_API_KEY")
API_SECRET = os.getenv("KITE_API_SECRET")

if not API_KEY or not API_SECRET:
    print("ERROR: KITE_API_KEY and KITE_API_SECRET must be set in .env first.")
    sys.exit(1)


def main():
    kite = KiteConnect(api_key=API_KEY)
    login_url = kite.login_url()

    print("\n=== Kite Connect Daily Token Refresh ===\n")
    print(f"Opening login page in your browser...")
    print(f"URL: {login_url}\n")
    webbrowser.open(login_url)

    print("After logging in, Zerodha will redirect to something like:")
    print("  http://localhost:8080/?request_token=XXXXXX&status=success\n")
    print("Copy the request_token value from that URL and paste it below.")
    print("(If the page shows an error, that's fine — just copy the token from the URL bar)\n")

    request_token = input("Paste request_token here: ").strip()

    # Strip any URL cruft if they pasted the full URL
    match = re.search(r"request_token=([A-Za-z0-9]+)", request_token)
    if match:
        request_token = match.group(1)

    try:
        data = kite.generate_session(request_token, api_secret=API_SECRET)
        access_token = data["access_token"]
    except Exception as e:
        print(f"\nERROR generating session: {e}")
        print("Double-check your API_KEY and API_SECRET in .env, then try again.")
        sys.exit(1)

    # Save to .env
    set_key(ENV_FILE, "KITE_ACCESS_TOKEN", access_token)
    print(f"\nSuccess! Access token saved to .env")
    print(f"Token (first 8 chars): {access_token[:8]}...")
    print("\nYou're ready to trade. The token is valid until 6 AM tomorrow.")


if __name__ == "__main__":
    main()
