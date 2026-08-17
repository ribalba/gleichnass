"""When to check, and how far ahead to look.

Every notification mode in this project is the same operation with different
parameters. "Tell me at 20:00 whether it rains overnight" and "tell me an hour
before it rains" differ only in their trigger and their look-ahead window, so
there is one Rule type rather than three special cases.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, tzinfo

from .analyze import Threshold
from .util import parse_duration, parse_time_of_day

PRESETS: dict[str, dict] = {
    "night": {"at": "20:00", "window": "12h", "notify_when_dry": True},
    "morning": {"at": "08:00", "window": "12h", "notify_when_dry": True},
    "imminent": {"every": "15m", "window": "1h", "notify_when_dry": False},
}


@dataclass
class RuleState:
    """What the store remembers about one rule for one user."""

    last_run_at: datetime | None = None
    last_notified_at: datetime | None = None
    last_event_start: datetime | None = None
    """Onset of the rain we last announced, so the same shower is announced once."""


@dataclass
class Rule:
    name: str
    window: timedelta
    at: time | None = None
    """Wall-clock trigger in the user's timezone, for digest-style rules."""
    every: timedelta | None = None
    """Interval trigger, for watch-style rules."""
    grace: timedelta = timedelta(hours=1)
    """How late a missed `at` rule may still fire. Stops a machine that was off
    all day from delivering last night's forecast over breakfast."""
    cooldown: timedelta = timedelta(minutes=30)
    """Hard floor between two notifications from this rule."""
    event_merge: timedelta = timedelta(hours=1)
    """Two onsets this close apart are the same shower, so only the first is sent."""
    notify_when_dry: bool = False
    min_agreement: int = 1
    """How many providers must expect rain before anyone's phone buzzes."""
    threshold: Threshold = field(default_factory=Threshold)
    providers: list[str] = field(default_factory=list)

    def __post_init__(self):
        if (self.at is None) == (self.every is None):
            raise ValueError(f"rule {self.name!r} needs exactly one of 'at' or 'every'")

    def is_due(self, now: datetime, zone: tzinfo, state: RuleState) -> bool:
        if self.every is not None:
            if state.last_run_at is None:
                return True
            # Tolerate a cron tick landing a few seconds early.
            return now - state.last_run_at >= self.every - timedelta(seconds=30)

        local = now.astimezone(zone)
        target = local.replace(
            hour=self.at.hour, minute=self.at.minute, second=0, microsecond=0
        )
        if not target <= local <= target + self.grace:
            return False
        if state.last_run_at is None:
            return True
        previous = state.last_run_at.astimezone(zone)
        return not (previous.date() == local.date() and previous >= target)

    def is_same_event(self, onset: datetime, state: RuleState) -> bool:
        if state.last_event_start is None:
            return False
        return abs(onset - state.last_event_start) <= self.event_merge


def build_rule(spec: dict, defaults: dict) -> Rule:
    """Turn one YAML entry into a Rule, applying a preset and the global defaults."""
    merged = dict(PRESETS.get(spec.get("preset", ""), {}))
    merged.update({key: value for key, value in spec.items() if value is not None})

    preset = merged.pop("preset", None)
    name = merged.pop("name", None) or preset
    if not name:
        raise ValueError("every rule needs a 'preset' or a 'name'")

    threshold = Threshold(
        min_mm_per_hour=float(merged.pop("min_mm_per_hour", defaults["min_mm_per_hour"])),
        min_probability=float(merged.pop("min_probability", defaults["min_probability"])),
        min_total_mm=float(merged.pop("min_total_mm", defaults["min_total_mm"])),
    )
    providers = merged.pop("providers", None) or defaults["providers"]

    known = {
        "window", "at", "every", "grace", "cooldown",
        "event_merge", "notify_when_dry", "min_agreement",
    }
    unknown = set(merged) - known
    if unknown:
        raise ValueError(f"rule {name!r} has unknown keys: {', '.join(sorted(unknown))}")

    return Rule(
        name=name,
        window=parse_duration(merged["window"]),
        at=parse_time_of_day(merged["at"]) if merged.get("at") else None,
        every=parse_duration(merged["every"]) if merged.get("every") else None,
        grace=parse_duration(merged.get("grace", "1h")),
        cooldown=parse_duration(merged.get("cooldown", "30m")),
        event_merge=parse_duration(merged.get("event_merge", "1h")),
        notify_when_dry=bool(merged.get("notify_when_dry", False)),
        min_agreement=int(merged.get("min_agreement", defaults["min_agreement"])),
        threshold=threshold,
        providers=list(providers),
    )
