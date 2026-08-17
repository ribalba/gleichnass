"""Small shared helpers."""

from __future__ import annotations

import os
import re
from datetime import time, timedelta

_DURATION = re.compile(r"(\d+(?:\.\d+)?)\s*(d|h|min|m|s)")
_UNITS = {"d": "days", "h": "hours", "m": "minutes", "min": "minutes", "s": "seconds"}
_ENV = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def parse_duration(text: str | timedelta) -> timedelta:
    """'12h', '90m', '2h30m' -> timedelta."""
    if isinstance(text, timedelta):
        return text
    parts = _DURATION.findall(str(text).strip().lower())
    if not parts:
        raise ValueError(f"cannot read duration {text!r} (try 12h, 90m, 2h30m)")
    total = timedelta()
    for amount, unit in parts:
        total += timedelta(**{_UNITS[unit]: float(amount)})
    return total


def parse_time_of_day(text: str | time) -> time:
    """'20:00' -> time."""
    if isinstance(text, time):
        return text
    try:
        hours, _, minutes = str(text).strip().partition(":")
        return time(int(hours), int(minutes or 0))
    except ValueError:
        raise ValueError(f"cannot read time of day {text!r} (try 20:00)") from None


def expand_env(value):
    """Replace ${VAR} in strings so secrets can live in the environment.

    Recurses through dicts and lists, which is how it reaches channel settings.
    """
    if isinstance(value, str):
        return _ENV.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {key: expand_env(item) for key, item in value.items()}
    if isinstance(value, list):
        return [expand_env(item) for item in value]
    return value


def format_duration(span: timedelta) -> str:
    minutes = max(0, int(span.total_seconds() // 60))
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h" if minutes == 0 else f"{hours}:{minutes:02d} h"
