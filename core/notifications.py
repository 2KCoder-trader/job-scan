"""Outbound notifications (Telegram for now)."""

import os
import requests

_TG_TOKEN   = os.environ.get("TELEGRAM_TOKEN")
_TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


def tg_send(text: str) -> None:
    """Send a Telegram message. Silent no-op if env vars missing."""
    if not _TG_TOKEN or not _TG_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{_TG_TOKEN}/sendMessage",
            json={"chat_id": _TG_CHAT_ID, "text": text,
                  "disable_web_page_preview": True},
            timeout=10,
        )
    except Exception as e:
        print(f"Telegram send failed: {e}")
