"""Loading `gleichnass.yaml` plus a directory of one-file-per-user configs.

Users live in their own files under `users.d/` so that the web signup can add
and remove people by writing whole files, without ever rewriting, and thereby
reformatting and stripping the comments from, the config you maintain by hand.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from . import notify
from .models import Location
from .rules import Rule, build_rule
from .util import expand_env

DEFAULTS = {
    "timezone": "Europe/Berlin",
    "language": "de",
    "min_mm_per_hour": 0.2,
    "min_probability": 50.0,
    "min_total_mm": 0.1,
    "min_agreement": 1,
    "providers": ["dwd-radar", "icon-d2", "brightsky"],
    # Settings shared by everyone on a given channel, keyed by channel type. A
    # user picks which channels they want; a Telegram user never sees the ntfy
    # server, and someone on both gets the right settings for each.
    "channels": {"ntfy": {"server": "https://ntfy.sh"}},
    # Assumed when a user names a channel without stating its type.
    "channel_type": "ntfy",
    # Tapping the notification should open something useful on a phone.
    "click_url": "https://www.windy.com/-Rain-thunder-rain?rain,{lat},{lon},9",
}


class ConfigError(Exception):
    pass


@dataclass
class Channel:
    type: str
    settings: dict = field(default_factory=dict)


@dataclass
class User:
    id: str
    name: str
    location: Location
    zone: ZoneInfo
    language: str
    channels: list[Channel]
    """Every notification goes to all of them, so ntfy and Telegram can run
    side by side."""
    rules: list[Rule]
    click_url: str = ""
    path: Path | None = None
    """The file this user came from, so `remove-user` knows what to delete."""


@dataclass
class Signup:
    enabled: bool = True
    """Open by default: the point is that friends sign themselves up."""
    invite_code: str | None = None
    """Optional. Set one to keep registration to people you gave it to."""
    per_hour: int = 8
    """Signups allowed from one address per hour. Registration is open to the
    whole internet, so something has to stop a script filling users.d/."""
    telegram_bot: str | None = None
    """Bot username, without the @. Set it and the site offers Telegram as a
    way to receive the alerts, linked by code instead of by hand."""
    base_url: str = ""
    title: str = "GleichNass"


@dataclass
class Config:
    path: Path
    users_dir: Path
    state_path: Path
    defaults: dict
    users: list[User]
    signup: Signup

    def user(self, user_id: str) -> User:
        for candidate in self.users:
            if candidate.id == user_id:
                return candidate
        raise KeyError(user_id)


def load(path: str | Path) -> Config:
    path = Path(path).expanduser()
    raw = _read_yaml(path) if path.exists() else {}
    if not path.exists():
        raise ConfigError(f"no config at {path}")

    raw_defaults = raw.get("defaults") or {}
    defaults = {**DEFAULTS, **raw_defaults}
    defaults["channels"] = _shared_channels(raw_defaults, defaults)

    base = path.parent
    users_dir = base / (raw.get("users_dir") or "users.d")
    # Relative to the config file, not to the working directory: the container
    # starts in /app while the config and its data live in a mounted /data.
    state_path = Path(raw.get("state_path") or "gleichnass.sqlite3").expanduser()
    if not state_path.is_absolute():
        state_path = base / state_path

    entries = [(path, entry) for entry in (raw.get("users") or [])]
    if users_dir.is_dir():
        for user_file in sorted(users_dir.glob("*.y*ml")):
            entries.append((user_file, _read_yaml(user_file)))

    users = []
    seen = set()
    for source, entry in entries:
        user = _build_user(entry, defaults, source)
        if user.id in seen:
            raise ConfigError(f"duplicate user id {user.id!r} (in {source})")
        seen.add(user.id)
        users.append(user)

    signup_raw = raw.get("signup") or {}
    signup = Signup(
        enabled=bool(signup_raw.get("enabled", True)),
        invite_code=expand_env(signup_raw.get("invite_code")) or None,
        per_hour=int(signup_raw.get("per_hour", 8)),
        telegram_bot=(signup_raw.get("telegram_bot") or "").lstrip("@") or None,
        base_url=(signup_raw.get("base_url") or "").rstrip("/"),
        title=signup_raw.get("title") or "GleichNass",
    )

    return Config(path, users_dir, state_path, defaults, users, signup)


def _shared_channels(raw_defaults: dict, defaults: dict) -> dict[str, dict]:
    """Per-type shared channel settings, from either config spelling.

    `defaults.channels` is a map of type to settings. `defaults.channel` is the
    older single-channel form; it folds into the same map and also decides the
    type users get when they do not name one.
    """
    shared = {name: dict(settings) for name, settings in DEFAULTS["channels"].items()}

    single = dict(raw_defaults.get("channel") or {})
    if single:
        channel_type = single.pop("type", None) or defaults["channel_type"]
        defaults["channel_type"] = channel_type
        shared[channel_type] = {**shared.get(channel_type, {}), **single}

    for channel_type, settings in (raw_defaults.get("channels") or {}).items():
        shared[channel_type] = {**shared.get(channel_type, {}), **(settings or {})}

    # Expand here, once, so everything downstream sees real values. Doing it
    # only per user left the shared settings holding a literal "${VAR}", which
    # anything reading the defaults directly then used verbatim.
    return {
        name: {k: v for k, v in expand_env(settings).items() if v != ""}
        for name, settings in shared.items()
    }


def _read_yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as error:
        raise ConfigError(f"{path}: {error}") from None
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping at the top level")
    return data


def _build_user(entry: dict, defaults: dict, source: Path) -> User:
    try:
        user_id = str(entry["id"])
        location_raw = entry["location"]
        location = Location(
            lat=float(location_raw["lat"]),
            lon=float(location_raw["lon"]),
            name=location_raw.get("name"),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ConfigError(
            f"{source}: user needs 'id' and 'location' with lat/lon ({error})"
        ) from None

    specs = entry.get("channels")
    if specs is None:
        specs = [entry["channel"]] if entry.get("channel") else []
    if not specs:
        raise ConfigError(f"{source}: user {user_id!r} has no channel")

    channels = []
    for spec in specs:
        spec = dict(spec or {})
        channel_type = spec.pop("type", None) or defaults["channel_type"]
        if channel_type not in notify.ALL:
            raise ConfigError(
                f"{source}: user {user_id!r} has unknown channel {channel_type!r};"
                f" available: {', '.join(notify.ALL)}"
            )
        settings = expand_env({**defaults["channels"].get(channel_type, {}), **spec})
        # An unset ${VAR} must not become an empty token that fails at send time.
        settings = {key: value for key, value in settings.items() if value != ""}
        channels.append(Channel(type=channel_type, settings=settings))

    rules_raw = entry.get("rules")
    if not rules_raw:
        raise ConfigError(f"{source}: user {user_id!r} has no rules")
    try:
        rules = [build_rule(rule, defaults) for rule in rules_raw]
    except ValueError as error:
        raise ConfigError(f"{source}: user {user_id!r}: {error}") from None

    zone_name = entry.get("timezone") or defaults["timezone"]
    try:
        zone = ZoneInfo(zone_name)
    except Exception:
        raise ConfigError(f"{source}: unknown timezone {zone_name!r}") from None

    return User(
        id=user_id,
        name=entry.get("name") or user_id,
        location=location,
        zone=zone,
        language=entry.get("language") or defaults["language"],
        channels=channels,
        rules=rules,
        click_url=(entry.get("click_url") or defaults["click_url"]).format(
            lat=location.lat, lon=location.lon
        ),
        path=source,
    )


def write_user(users_dir: Path, entry: dict) -> Path:
    """Persist one user as their own YAML file, atomically."""
    users_dir.mkdir(parents=True, exist_ok=True)
    target = users_dir / f"{entry['id']}.yaml"
    body = yaml.safe_dump(entry, allow_unicode=True, sort_keys=False)
    handle, temporary = tempfile.mkstemp(dir=users_dir, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(body)
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return target
