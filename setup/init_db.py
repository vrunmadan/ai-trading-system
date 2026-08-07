"""
Run this once to initialise the SQLite ledger before first use.

    python setup/init_db.py

Safe to run again — uses CREATE TABLE IF NOT EXISTS throughout.
"""

import sys
import os

# Allow running from project root
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv()

from ledger.db import init_db

if __name__ == "__main__":
    init_db()
    print("Done. You can now run the scheduler or main.py in paper mode.")
