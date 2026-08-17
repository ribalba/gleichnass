"""What every delivery channel has to implement."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

import httpx


@dataclass
class Notification:
    title: str
    body: str
    priority: int = 3
    """1 (min) to 5 (max), following ntfy's scale; other channels map onto it."""
    tags: list[str] = field(default_factory=list)
    click: str | None = None


class Channel(Protocol):
    type: str

    def describe(self) -> str:
        """One line for logs and `gleichnass users`."""
        ...

    def send(self, client: httpx.Client, notification: Notification) -> None:
        """Deliver, or raise."""
        ...
