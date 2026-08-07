"""
Set Telegram webhook — run once after deploying to Railway (or ngrok for local testing).

Usage:
    python setup/set_telegram_webhook.py https://your-app.railway.app

Telegram will then POST all bot updates to:
    https://your-app.railway.app/telegram_webhook

To check the current webhook:
    python setup/set_telegram_webhook.py --info

To delete the webhook (go back to polling):
    python setup/set_telegram_webhook.py --delete
"""

import os
import sys
import requests
from dotenv import load_dotenv

load_dotenv()


def get_token() -> str:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        print("❌  TELEGRAM_BOT_TOKEN not set in .env")
        sys.exit(1)
    return token


def set_webhook(base_url: str) -> None:
    token = get_token()
    webhook_url = f"{base_url.rstrip('/')}/telegram_webhook"

    r = requests.post(
        f"https://api.telegram.org/bot{token}/setWebhook",
        json={
            "url": webhook_url,
            "allowed_updates": ["callback_query"],   # only what we need
            "drop_pending_updates": True,            # start clean
        },
        timeout=15,
    )
    data = r.json()
    if data.get("ok"):
        print(f"✓  Webhook set: {webhook_url}")
        print(f"   Telegram will POST callback_query updates to this URL.")
    else:
        print(f"❌  setWebhook failed: {data}")
        sys.exit(1)


def get_info() -> None:
    token = get_token()
    r = requests.get(f"https://api.telegram.org/bot{token}/getWebhookInfo", timeout=10)
    import json
    print(json.dumps(r.json(), indent=2))


def delete_webhook() -> None:
    token = get_token()
    r = requests.post(
        f"https://api.telegram.org/bot{token}/deleteWebhook",
        json={"drop_pending_updates": True},
        timeout=10,
    )
    data = r.json()
    if data.get("ok"):
        print("✓  Webhook deleted. Bot is now in polling mode.")
    else:
        print(f"❌  deleteWebhook failed: {data}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    arg = sys.argv[1]
    if arg == "--info":
        get_info()
    elif arg == "--delete":
        delete_webhook()
    elif arg.startswith("http"):
        set_webhook(arg)
    else:
        print(f"Unknown argument: {arg}")
        print("Usage: python setup/set_telegram_webhook.py https://your-app.railway.app")
        sys.exit(1)
