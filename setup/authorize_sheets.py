"""
One-time Google Sheets OAuth2 authorisation.

Run this script ONCE from the project root before starting the trading system:

    python setup/authorize_sheets.py

What it does:
  1. Opens a browser tab (Google login page).
  2. You log in with your Google account (vrunmadan@gmail.com).
  3. Google redirects back to localhost — the script captures the token.
  4. The token is saved to credentials/authorized_user.json.

After this, the pipeline authenticates silently using the cached token.
gspread auto-refreshes it before it expires — you should never need to
re-run this unless you delete the token file.

Scopes requested:
  - spreadsheets    (read/write your Google Sheets)
  - drive.file      (open spreadsheets by ID)
"""

import os
import sys

# Ensure we can import from the project root regardless of cwd
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from dotenv import load_dotenv
load_dotenv()

OAUTH_CLIENT_PATH    = os.getenv("SHEETS_OAUTH_CLIENT_PATH", "credentials/oauth_client.json")
AUTHORIZED_USER_PATH = os.getenv("SHEETS_AUTHORIZED_USER_PATH", "credentials/authorized_user.json")

# Resolve relative paths from project root
if not os.path.isabs(OAUTH_CLIENT_PATH):
    OAUTH_CLIENT_PATH = os.path.join(project_root, OAUTH_CLIENT_PATH)
if not os.path.isabs(AUTHORIZED_USER_PATH):
    AUTHORIZED_USER_PATH = os.path.join(project_root, AUTHORIZED_USER_PATH)


def main():
    if not os.path.exists(OAUTH_CLIENT_PATH):
        print(f"ERROR: OAuth client file not found at {OAUTH_CLIENT_PATH}")
        print("Make sure credentials/oauth_client.json exists in the project root.")
        sys.exit(1)

    print("Opening browser for Google authentication...")
    print(f"OAuth client : {OAUTH_CLIENT_PATH}")
    print(f"Token output : {AUTHORIZED_USER_PATH}")
    print()

    try:
        import gspread
        # gspread.oauth() opens the browser and handles the callback automatically
        client = gspread.oauth(
            credentials_filename=OAUTH_CLIENT_PATH,
            authorized_user_filename=AUTHORIZED_USER_PATH,
        )
        # Quick sanity check: list the user's spreadsheets
        sheets = client.list_spreadsheet_files()
        print(f"\n✅ Authentication successful!")
        print(f"   Token saved to: {AUTHORIZED_USER_PATH}")
        print(f"   Found {len(sheets)} spreadsheet(s) in your Google Drive.")
        print()
        print("Next step: create a Google Sheet, then add its ID to .env:")
        print("   SHEETS_SPREADSHEET_ID=<paste the ID from the Sheet URL here>")
        print()
        print("The Sheet URL looks like:")
        print("   https://docs.google.com/spreadsheets/d/<SPREADSHEET_ID>/edit")

    except ImportError:
        print("ERROR: gspread is not installed. Run: pip install gspread")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: Authentication failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
