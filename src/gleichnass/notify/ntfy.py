"""ntfy, free push to Android and iOS without shipping an app.

Publishing is an HTTP POST, and subscribing is installing the ntfy app and
entering a topic name (or just opening the topic URL in a browser). There are no
accounts and no keys, which is what makes handing this to a friend a one-minute
job.

The topic name is the only secret on a public server: anyone who knows it can
read and post to it. Topics are therefore generated with 128 bits of randomness
rather than being chosen by hand, see `gleichnass.web`.
"""

from __future__ import annotations

import secrets

import httpx

from .base import Notification


def new_topic(prefix: str = "gleichnass") -> str:
    """A topic nobody will guess: 128 bits, in ntfy's allowed character set."""
    return f"{prefix}-{secrets.token_urlsafe(16)}"


class NtfyChannel:
    type = "ntfy"

    def __init__(self, topic: str, server: str = "https://ntfy.sh", token: str | None = None):
        if not topic:
            raise ValueError("ntfy channel needs a 'topic'")
        self.topic = topic
        self.server = server.rstrip("/")
        self.token = token or None

    @property
    def url(self) -> str:
        return f"{self.server}/{self.topic}"

    def describe(self) -> str:
        return f"ntfy {self.server}/{self.topic[:6]}…"

    def send(self, client: httpx.Client, notification: Notification) -> None:
        # The JSON endpoint rather than the header-based one: ntfy's headers are
        # latin-1, and these messages contain degrees, arrows and umlauts.
        payload = {
            "topic": self.topic,
            "title": notification.title,
            "message": notification.body,
            "priority": notification.priority,
        }
        if notification.tags:
            payload["tags"] = notification.tags
        if notification.click:
            payload["click"] = notification.click

        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        response = client.post(self.server, json=payload, headers=headers)
        response.raise_for_status()
