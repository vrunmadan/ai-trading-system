"""
Connection test — run this after filling in your .env file to verify
all six integrations are working before going anywhere near paper mode.

    python setup/test_connections.py

Each test is independent. A failure on one doesn't stop the others.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv()

results = {}


def test(name):
    def decorator(fn):
        try:
            fn()
            results[name] = "✓  OK"
        except Exception as e:
            results[name] = f"✗  FAILED — {e}"
        return fn
    return decorator


@test("Kite Connect (profile fetch)")
def check_kite():
    from kiteconnect import KiteConnect
    api_key = os.getenv("KITE_API_KEY")
    access_token = os.getenv("KITE_ACCESS_TOKEN")
    assert api_key and access_token, "KITE_API_KEY or KITE_ACCESS_TOKEN missing in .env"
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)
    profile = kite.profile()
    assert profile.get("user_name"), "Empty profile returned"
    print(f"   Logged in as: {profile['user_name']} ({profile['email']})")


@test("Anthropic (Claude Sonnet 5 — Researcher)")
def check_anthropic():
    import anthropic
    key = os.getenv("ANTHROPIC_API_KEY")
    assert key, "ANTHROPIC_API_KEY missing in .env"
    client = anthropic.Anthropic(api_key=key)
    msg = client.messages.create(
        model=os.getenv("RESEARCHER_MODEL", "claude-sonnet-5"),
        max_tokens=10,
        messages=[{"role": "user", "content": "Reply with: OK"}],
    )
    assert msg.content, "No response from Anthropic"


@test("OpenAI (GPT-5.5 — QC/Fact-Checker)")
def check_openai():
    from openai import OpenAI
    key = os.getenv("OPENAI_API_KEY")
    assert key, "OPENAI_API_KEY missing in .env"
    client = OpenAI(api_key=key)
    resp = client.chat.completions.create(
        model=os.getenv("QC_MODEL", "gpt-5.5"),
        max_tokens=10,
        messages=[{"role": "user", "content": "Reply with: OK"}],
    )
    assert resp.choices, "No response from OpenAI"


@test("Google AI Studio (Gemini 3.5 Pro — Auditor)")
def check_google():
    import google.generativeai as genai
    key = os.getenv("GOOGLE_API_KEY")
    assert key, "GOOGLE_API_KEY missing in .env"
    genai.configure(api_key=key)
    model = genai.GenerativeModel(os.getenv("AUDITOR_MODEL", "gemini-3.5-pro"))
    resp = model.generate_content("Reply with: OK")
    assert resp.text, "No response from Google"


@test("Telegram bot (send test message)")
def check_telegram():
    import requests
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    assert token and chat_id, "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing in .env"
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": "AI Trading System: connection test OK ✓"},
    )
    assert r.ok and r.json().get("ok"), f"Telegram error: {r.text}"


@test("Ledger (SQLite init)")
def check_ledger():
    from ledger.db import init_db, get_db
    init_db()
    with get_db() as conn:
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    assert len(tables) >= 3, f"Expected ≥3 tables, got {len(tables)}"


if __name__ == "__main__":
    print("\n=== AI Trading System — Connection Tests ===\n")
    # Tests run via decorators above
    print("\nResults:")
    for name, result in results.items():
        print(f"  {result}  [{name}]")

    failed = [n for n, r in results.items() if r.startswith("✗")]
    if failed:
        print(f"\n{len(failed)} test(s) failed. Fix the issues above, then re-run.")
        sys.exit(1)
    else:
        print("\nAll connections OK. You're ready to run paper mode.")
