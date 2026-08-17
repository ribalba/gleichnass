"""Connecting a Telegram chat to a user without anyone editing a file.

Telegram will not tell you who someone is until they message the bot, and the
bot cannot know which website visitor the message belongs to. So signup mints a
short code, the person sends it to the bot (one tap via a t.me deep link), and
the next run matches the code and writes the chat id into their user file.

The code lives in the user's own YAML rather than a separate table, so a
half-finished link survives a restart and is obvious when you look at the file.
"""

from __future__ import annotations

import logging
import secrets

import httpx
import yaml

from .config import Config, write_user
from .notify.telegram import recent_messages

log = logging.getLogger("gleichnass.telegram")

# No I, O, S, Z, 0, 1, 2, 5, 8: nobody should have to guess whether that is a
# letter or a digit while reading a code off a screen.
ALPHABET = "ACDEFGHJKLMNPQRTUVWXY34679"
CODE_LENGTH = 6

PENDING_KEY = "telegram_code"


def new_code() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))


def deep_link(bot: str, code: str) -> str:
    """One tap opens the bot with the code already filled in."""
    return f"https://t.me/{bot.lstrip('@')}?start={code}"


def pending(config: Config) -> dict[str, str]:
    """Codes still waiting to be claimed, by user id."""
    found = {}
    for user in config.users:
        if user.path is None or user.path == config.path:
            continue
        raw = yaml.safe_load(user.path.read_text(encoding="utf-8")) or {}
        code = raw.get(PENDING_KEY)
        if code:
            found[user.id] = str(code).upper()
    return found


def link_waiting(config: Config, client: httpx.Client, token: str) -> list[tuple[str, int]]:
    """Attach a Telegram chat to every user whose code has been sent to the bot.

    Returns the (user id, chat id) pairs that were linked.
    """
    waiting = pending(config)
    if not waiting:
        return []

    try:
        messages = recent_messages(client, token)
    except Exception as error:  # noqa: BLE001 - a Telegram outage must not stop the run
        log.warning("could not read Telegram updates: %s", error)
        return []

    by_code = {code: user_id for user_id, code in waiting.items()}
    linked = []
    for message in messages:
        text = (message["text"] or "").upper()
        for code, user_id in list(by_code.items()):
            if code not in text:
                continue
            if _attach(config, user_id, message["chat_id"]):
                linked.append((user_id, message["chat_id"]))
                log.info("linked Telegram chat %s to %s", message["chat_id"], user_id)
            by_code.pop(code, None)
    return linked


def _attach(config: Config, user_id: str, chat_id: int) -> bool:
    user = next((u for u in config.users if u.id == user_id), None)
    if user is None or user.path is None:
        return False

    entry = yaml.safe_load(user.path.read_text(encoding="utf-8")) or {}
    entry.pop(PENDING_KEY, None)

    channels = entry.get("channels")
    if channels is None:
        channels = [entry.pop("channel")] if entry.get("channel") else []
    if any(str(c.get("chat_id")) == str(chat_id) for c in channels if c.get("type") == "telegram"):
        return False  # already linked; nothing to write

    channels.append({"type": "telegram", "chat_id": chat_id})
    entry["channels"] = channels
    write_user(config.users_dir, entry)
    return True
