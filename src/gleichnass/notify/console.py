"""Prints instead of sending. What --dry-run swaps in, and a usable channel in
its own right when running the checker in a terminal."""

from __future__ import annotations

import sys

import httpx

from .base import Notification


class ConsoleChannel:
    type = "console"

    def __init__(self, stream=None, prefix: str = ""):
        self.stream = stream or sys.stdout
        self.prefix = prefix

    def describe(self) -> str:
        return "console"

    def send(self, client: httpx.Client, notification: Notification) -> None:
        tags = f"  [{', '.join(notification.tags)}]" if notification.tags else ""
        body = "\n".join(f"      {line}" for line in notification.body.splitlines())
        print(
            f"{self.prefix}  » {notification.title}{tags}\n{body}",
            file=self.stream,
            flush=True,
        )
