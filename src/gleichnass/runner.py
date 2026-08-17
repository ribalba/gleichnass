"""One pass over every user and every rule: check what is due, decide, deliver.

Designed to be run from cron every few minutes rather than to run forever. There
is no in-memory state, so a restart, a missed tick or a machine that was asleep
all afternoon costs nothing, everything worth remembering is in SQLite.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from . import config as config_module
from . import notify, providers
from .analyze import Consensus, Outlook, analyze, consensus
from .config import Config, User
from .message import render
from .models import Forecast, Location
from .notify import Notification
from .notify.telegram import Blocked
from .rules import Rule
from .state import Store

log = logging.getLogger("gleichnass")


@dataclass
class Decision:
    user: User
    rule: Rule
    reason: str
    verdict: Consensus | None = None
    notification: Notification | None = None
    sent: bool = False
    error: str | None = None

    def __str__(self) -> str:
        head = f"{self.user.id}/{self.rule.name}: {self.reason}"
        return f"{head}, {self.error}" if self.error else head


def run_once(
    config: Config,
    store: Store,
    client: httpx.Client,
    now: datetime | None = None,
    *,
    dry_run: bool = False,
    only_user: str | None = None,
    force: bool = False,
) -> list[Decision]:
    now = now or datetime.now(UTC)
    users = [u for u in config.users if only_user in (None, u.id)]

    due = [
        (user, rule)
        for user in users
        for rule in user.rules
        if force or rule.is_due(now, user.zone, store.get(user.id, rule.name))
    ]
    if not due:
        return []

    forecasts = _fetch_all(client, due, now)
    return [
        _handle(config, store, client, user, rule, forecasts, now, dry_run)
        for user, rule in due
    ]


def _fetch_all(client, due, now) -> dict[tuple, Forecast | Exception]:
    """One request per (provider, place), however many users share it."""
    needed: dict[tuple, tuple[Location, datetime]] = {}
    for user, rule in due:
        until = now + rule.window
        for provider in rule.providers:
            key = (provider, round(user.location.lat, 4), round(user.location.lon, 4))
            location, previous = needed.get(key, (user.location, until))
            needed[key] = (location, max(previous, until))

    def fetch(item):
        key, (location, until) = item
        try:
            return key, providers.build(key[0]).fetch(client, location, until)
        except Exception as error:  # noqa: BLE001, one dead API must not stop the rest
            log.warning("provider %s failed for %s: %s", key[0], location, error)
            return key, error

    with ThreadPoolExecutor(max_workers=min(8, len(needed))) as pool:
        return dict(pool.map(fetch, needed.items()))


def _handle(config, store, client, user, rule, forecasts, now, dry_run) -> Decision:
    outlooks: list[Outlook] = []
    for provider in rule.providers:
        forecast = forecasts.get((provider, round(user.location.lat, 4), round(user.location.lon, 4)))
        if isinstance(forecast, Forecast):
            outlooks.append(analyze(forecast, now, now + rule.window, rule.threshold))

    verdict = consensus(outlooks, rule.min_agreement)
    state = store.get(user.id, rule.name)

    if verdict.answering == 0:
        # Do not burn an `at` rule's once-a-day slot on an outage; the grace
        # window will bring it back on the next tick.
        if rule.every is not None and not dry_run:
            state.last_run_at = now
            store.put(user.id, rule.name, state)
        return Decision(user, rule, "no provider data", verdict)

    if not dry_run:
        state.last_run_at = now
        store.put(user.id, rule.name, state)

    if not verdict.will_rain:
        if not rule.notify_when_dry:
            return Decision(user, rule, "dry, staying quiet", verdict)
    elif rule.every is not None:
        # Watch-style rules fire repeatedly, so they need to recognise a shower
        # they have already announced. Digests run once a day and do not.
        if state.last_notified_at and now - state.last_notified_at < rule.cooldown:
            return Decision(user, rule, "within cooldown", verdict)
        if rule.is_same_event(verdict.leading.onset, state):
            return Decision(user, rule, "already announced this shower", verdict)

    notification = render(user, rule, verdict, now)
    channels, problems = _channels(user, rule, dry_run)
    if not channels:
        return Decision(user, rule, "no usable channel", verdict, error="; ".join(problems))

    delivered, errors = [], list(problems)
    for channel in channels:
        try:
            channel.send(client, notification)
        except Blocked as error:
            # They blocked the bot. Nothing will ever arrive again, so stop
            # carrying the channel rather than failing on it every tick.
            message = f"{channel.type}: blocked ({error})"
            log.info("dropping blocked channel for %s: %s", user.id, error)
            errors.append(message)
            if not dry_run:
                config_module.drop_channel(
                    config, user, "telegram", getattr(channel, "chat_id", "")
                )
        except Exception as error:  # noqa: BLE001
            message = f"{channel.type}: {type(error).__name__}: {error}"
            log.error("delivery to %s failed: %s", user.id, message)
            errors.append(message)
        else:
            delivered.append(channel)
        if not dry_run:
            store.log_delivery(
                user.id, rule.name, channel.type, notification.title, notification.body,
                ok=channel in delivered, error=errors[-1] if channel not in delivered else None,
            )

    if not delivered:
        return Decision(user, rule, "delivery failed", verdict, notification,
                        error="; ".join(errors))

    if not dry_run:
        # Remembered once it has reached at least one device. A channel that
        # failed misses this shower rather than the working one being told about
        # it again on every tick until the broken one recovers.
        state.last_notified_at = now
        state.last_event_start = verdict.leading.onset if verdict.will_rain else None
        store.put(user.id, rule.name, state)

    reason = "would send" if dry_run else "sent"
    if errors:
        reason += f" to {len(delivered)} of {len(delivered) + len(errors)} channels"
    return Decision(user, rule, reason, verdict, notification, sent=True,
                    error="; ".join(errors) or None)


def _channels(user, rule, dry_run) -> tuple[list, list[str]]:
    """Every channel that can be built, plus a note about each that cannot, one person's broken Telegram token must not cost them their ntfy alerts."""
    if dry_run:
        return [notify.ConsoleChannel(prefix=f"    {user.id}/{rule.name}")], []

    built, problems = [], []
    for spec in user.channels:
        try:
            built.append(notify.build(spec.type, spec.settings))
        except ValueError as error:
            log.error("channel %s for %s is unusable: %s", spec.type, user.id, error)
            problems.append(f"{spec.type}: {error}")
    return built, problems
