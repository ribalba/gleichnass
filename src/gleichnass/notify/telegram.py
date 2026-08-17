"""Telegram, for friends who would rather not install another app.

One bot serves everyone. Each person sends it /start, which is how you learn
their chat id; `gleichnass telegram-ids` reads those back out of the bot's
update queue so nobody has to look up a numeric id by hand.

The bot token is shared across all users and belongs in the environment, not in
a user file: write `token: ${GLEICHNASS_TELEGRAM_TOKEN}` in the defaults.
"""

from __future__ import annotations

import httpx

from .base import Notification

API = "https://api.telegram.org"


class TelegramChannel:
    type = "telegram"

    # Defaulted rather than required so the checks below produce the error,
    # instead of Python reporting a missing argument at people.
    def __init__(self, chat_id: str | int = "", token: str = "", server: str = API):
        if not token:
            raise ValueError(
                "telegram channel needs a 'token', set GLEICHNASS_TELEGRAM_TOKEN"
            )
        if not chat_id:
            raise ValueError("telegram channel needs a 'chat_id'")
        self.chat_id = str(chat_id)
        self.token = token
        self.server = server.rstrip("/")

    def describe(self) -> str:
        return f"telegram chat {self.chat_id}"

    def send(self, client: httpx.Client, notification: Notification) -> None:
        response = client.post(
            f"{self.server}/bot{self.token}/sendMessage",
            json={
                "chat_id": self.chat_id,
                "text": f"{notification.title}\n{notification.body}",
                # Telegram has no priority; a low-priority note arrives silently.
                "disable_notification": notification.priority <= 2,
                "link_preview_options": {"is_disabled": True},
            },
        )
        response.raise_for_status()


def recent_messages(client: httpx.Client, token: str, server: str = API) -> list[dict]:
    """Recent inbound messages as {text, chat_id, chat}.

    No offset is tracked, so the same update may be seen more than once.
    Everything built on this has to be idempotent, which linking is: the code
    disappears from the user file as soon as it is used.
    """
    response = client.get(f"{server}/bot{token}/getUpdates", params={"limit": 100})
    response.raise_for_status()
    out = []
    for update in response.json().get("result", []):
        message = update.get("message") or update.get("edited_message") or {}
        chat = message.get("chat")
        if chat:
            out.append({"text": message.get("text") or "", "chat_id": chat["id"], "chat": chat})
    return out


def recent_chats(client: httpx.Client, token: str, server: str = API) -> list[dict]:
    """Everyone who has messaged the bot recently, for onboarding by hand."""
    chats = {m["chat_id"]: m["chat"] for m in recent_messages(client, token, server)}
    return list(chats.values())
