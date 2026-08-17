"""Registry of delivery channels."""

from __future__ import annotations

from .base import Channel, Notification
from .console import ConsoleChannel
from .ntfy import NtfyChannel
from .telegram import TelegramChannel

_FACTORIES = {
    "ntfy": NtfyChannel,
    "telegram": TelegramChannel,
    "console": ConsoleChannel,
}

ALL = list(_FACTORIES)


def build(channel_type: str, settings: dict) -> Channel:
    try:
        factory = _FACTORIES[channel_type]
    except KeyError:
        raise ValueError(f"unknown channel {channel_type!r}; available: {', '.join(ALL)}") from None
    try:
        return factory(**settings)
    except TypeError as error:
        raise ValueError(f"bad settings for channel {channel_type!r}: {error}") from None


__all__ = ["ALL", "Channel", "Notification", "build"]
