"""Thin Bot API sender used by the pipeline (digests, alerts).

Raw sendMessage over httpx — the persistent python-telegram-bot daemon
(bot.py) exists only to RECEIVE updates; sending never needs it (E2).
"""

import json
import os

import httpx

API = "https://api.telegram.org/bot{token}/{method}"


class Telegram:
    def __init__(self, token: str | None = None, chat_id: str | None = None):
        self.token = token or os.environ["TELEGRAM_BOT_TOKEN"]
        self.chat_id = chat_id or os.environ["TELEGRAM_CHAT_ID"]

    def send(self, payload: dict, client: httpx.Client | None = None) -> bool:
        payload = dict(payload, chat_id=self.chat_id)
        if isinstance(payload.get("reply_markup"), dict):
            payload["reply_markup"] = json.dumps(payload["reply_markup"])
        close = client is None
        client = client or httpx.Client(timeout=15)
        try:
            resp = client.post(API.format(token=self.token, method="sendMessage"), json=payload)
            return resp.status_code == 200 and resp.json().get("ok", False)
        finally:
            if close:
                client.close()

    def send_all(self, payloads: list[dict]) -> int:
        with httpx.Client(timeout=15) as client:
            return sum(1 for p in payloads if self.send(p, client))
