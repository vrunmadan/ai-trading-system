"""
Encode Google Sheets credentials for Railway deployment.

Run once from the project root:
    python setup/export_sheets_credentials.py

Prints two Railway env var values to copy-paste.
The credentials themselves never leave your machine — only the encoded version
goes into Railway, and only Railway reads them.
"""

import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from dotenv import load_dotenv
load_dotenv()

OAUTH_CLIENT_PATH    = os.getenv("SHEETS_OAUTH_CLIENT_PATH", "credentials/oauth_client.json")
AUTHORIZED_USER_PATH = os.getenv("SHEETS_AUTHORIZED_USER_PATH", "credentials/authorized_user.json")


def encode_file(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def main():
    errors = []
    for path in (OAUTH_CLIENT_PATH, AUTHORIZED_USER_PATH):
        if not os.path.exists(path):
            errors.append(f"  Missing: {path}")
    if errors:
        print("ERROR — credential files not found:")
        print("\n".join(errors))
        print("\nRun `python setup/authorize_sheets.py` first to generate them.")
        sys.exit(1)

    oauth_b64 = encode_file(OAUTH_CLIENT_PATH)
    auth_b64  = encode_file(AUTHORIZED_USER_PATH)

    print("\n" + "═" * 64)
    print("  Add these two variables to Railway → Variables")
    print("═" * 64)
    print()
    print("Variable name:   SHEETS_OAUTH_CLIENT_B64")
    print(f"Variable value:  {oauth_b64}")
    print()
    print("Variable name:   SHEETS_AUTHORIZED_USER_B64")
    print(f"Variable value:  {auth_b64}")
    print()
    print("═" * 64)
    print("Done. Railway will redeploy automatically after you save.")
    print()


if __name__ == "__main__":
    main()
