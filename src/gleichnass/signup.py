"""Creating a user, shared by `gleichnass add-user` and the web signup.

Both routes must produce byte-identical user files, so the logic lives here
rather than being written twice.
"""

from __future__ import annotations

import re
import secrets
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx

from datetime import timedelta

from . import geocode
from .config import Config, write_user
from .models import Location
from .notify.ntfy import new_topic
from .rules import PRESETS
from .util import parse_duration, parse_time_of_day

DEFAULT_PRESETS = ["night", "morning", "imminent"]

# Which field of a preset a person may set for themselves when signing up.
# Digests happen at a wall-clock time; the watch has no clock, only how far
# ahead it looks.
ADJUSTABLE = {"night": "at", "morning": "at", "imminent": "window"}

MIN_WINDOW = timedelta(minutes=15)
MAX_WINDOW = timedelta(hours=12)


@dataclass
class Signup:
    entry: dict
    path: Path
    topic: str | None
    location: Location


def slugify(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-") or "friend"


def new_id() -> str:
    """A user's permanent identity.

    Deliberately not derived from their name. Two friends called Alex would
    otherwise become "alex" and "alex-2", which tells neither of them apart,
    turns removing one into a guess, and makes the id something a stranger can
    type. The name they gave is kept alongside as the label people actually
    read.
    """
    return str(uuid.uuid4())


def rule_entry(preset: str, value: str | None = None) -> dict:
    """One rule for a user file, with the person's own time filled in.

    Written out even when it matches the preset, so their file says plainly
    what they asked for rather than leaving it implied.
    """
    entry = {"preset": preset}
    field = ADJUSTABLE.get(preset)
    if not field:
        return entry

    if field == "at":
        chosen = parse_time_of_day(value) if value else parse_time_of_day(PRESETS[preset]["at"])
        entry["at"] = chosen.strftime("%H:%M")
    else:
        window = parse_duration(value) if value else parse_duration(PRESETS[preset]["window"])
        if not MIN_WINDOW <= window <= MAX_WINDOW:
            raise ValueError("window must be between 15 minutes and 12 hours")
        entry["window"] = format_window(window)
    return entry


def format_window(window: timedelta) -> str:
    minutes = int(window.total_seconds() // 60)
    return f"{minutes // 60}h" if minutes % 60 == 0 else f"{minutes}m"


def create(
    config: Config,
    *,
    name: str,
    client: httpx.Client,
    place: str | None = None,
    location: Location | None = None,
    presets: list[str] | None = None,
    # Per-preset override keyed by preset name: a clock time for the digests
    # ("20:00"), a duration for the watch ("90m").
    options: dict[str, str] | None = None,
    language: str | None = None,
    timezone: str | None = None,
    channels: list[dict] | None = None,
    telegram_code: str | None = None,
    user_id: str | None = None,
) -> Signup:
    if location is None:
        if not place:
            raise ValueError("need either a place name or coordinates")
        location = geocode.lookup(client, place)

    user_id = slugify(user_id) if user_id else None
    if user_id and any(u.id == user_id for u in config.users):
        raise ValueError(f"user id {user_id!r} is already taken")

    presets = presets or DEFAULT_PRESETS
    unknown = [p for p in presets if p not in PRESETS]
    if unknown:
        raise ValueError(f"unknown mode(s): {', '.join(unknown)}")

    channels = [dict(spec) for spec in channels] if channels else [{"type": "ntfy"}]
    topic = None
    for spec in channels:
        # Every ntfy channel needs a topic nobody can guess; generate any that
        # were not supplied. The first is what the QR code points at.
        if spec.get("type", "ntfy") == "ntfy" and not spec.get("topic"):
            spec["topic"] = new_topic()
        if spec.get("type", "ntfy") == "ntfy" and topic is None:
            topic = spec["topic"]

    entry = {
        "id": user_id or new_id(),
        "name": name,
        "location": {
            "lat": round(location.lat, 5),
            "lon": round(location.lon, 5),
            "name": location.name or place or name,
        },
        "language": language or config.defaults["language"],
        "timezone": timezone or config.defaults["timezone"],
        "channels": channels,
        # Their own key for leaving. ntfy cannot tell us that someone
        # unsubscribed in the app, so the only way out is one they can take.
        "unsubscribe": secrets.token_urlsafe(12),
        "rules": [rule_entry(preset, (options or {}).get(preset)) for preset in presets],
    }
    if telegram_code:
        # Claimed by the next run, once the person has messaged the bot.
        entry["telegram_code"] = telegram_code
    return Signup(entry, write_user(config.users_dir, entry), topic, location)
