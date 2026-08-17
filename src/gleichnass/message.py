"""Turning a consensus into the two lines that land on a lock screen.

Kept apart from delivery and from the weather logic so the wording can be
changed, or translated, without touching either.
"""

from __future__ import annotations

from datetime import datetime, timedelta, tzinfo

from .analyze import Consensus
from .notify import Notification
from .rules import PRESETS, Rule

TEXTS = {
    "de": {
        "rain_in": "Regen in {delta}",
        "rain_now": "Es fängt gleich an zu regnen",
        "rain_window": "Regen {label}",
        "dry_window": "Kein Regen {label}",
        "labels": {"night": "heute Nacht", "morning": "heute", "imminent": "in Kürze"},
        "from_until": "{start}–{end}",
        "from_open": "ab {start}",
        "totals": "{mm:.1f} mm, Spitze {peak:.1f} mm/h",
        "more_spells": "danach noch {count} Schauer",
        "dry_until": "Trocken bis {end}.",
        "agreement": "{agreeing} von {answering} Diensten",
        "only_sees": "Vorhersage reicht nur bis {end}",
        "minutes": "{count} Min",
        "hours": "{hours}:{minutes:02d} Std",
        "hours_round": "{hours} Std",
        "test_title": "Whoooo, es funktioniert!",
        "test_body": "Ab jetzt meldet sich GleichNass bei dir:\n{rules}",
        "rule_imminent": "Immer wenn es gleich regnet",
        "rule_night": "Jeden Abend um {at}, für die Nacht",
        "rule_morning": "Jeden Morgen um {at}, für den Tag",
        "rule_daily": "Jeden Tag um {at}",
        "rule_bullet": "· {line}",
        "unsubscribe": "Abmelden",
    },
    "en": {
        "rain_in": "Rain in {delta}",
        "rain_now": "Rain starting now",
        "rain_window": "Rain {label}",
        "dry_window": "No rain {label}",
        "labels": {"night": "tonight", "morning": "today", "imminent": "shortly"},
        "from_until": "{start}–{end}",
        "from_open": "from {start}",
        "totals": "{mm:.1f} mm, peak {peak:.1f} mm/h",
        "more_spells": "then {count} more shower(s)",
        "dry_until": "Dry until {end}.",
        "agreement": "{agreeing} of {answering} providers",
        "only_sees": "forecast only reaches {end}",
        "minutes": "{count} min",
        "hours": "{hours}:{minutes:02d} h",
        "hours_round": "{hours} h",
        "test_title": "Whoooo, it works!",
        "test_body": "From now on GleichNass will tell you:\n{rules}",
        "rule_imminent": "Whenever rain is about to start",
        "rule_night": "Every evening at {at}, for the night",
        "rule_morning": "Every morning at {at}, for the day",
        "rule_daily": "Every day at {at}",
        "rule_bullet": "· {line}",
        "unsubscribe": "Unsubscribe",
    },
}


def texts(language: str) -> dict:
    return TEXTS.get(language, TEXTS["en"])


def render(
    user,
    rule: Rule,
    verdict: Consensus,
    now: datetime,
) -> Notification:
    words = texts(user.language)
    zone = user.zone
    place = user.location.name or f"{user.location.lat:.3f}, {user.location.lon:.3f}"
    label = words["labels"].get(rule.name, rule.name)

    if not verdict.will_rain:
        end = verdict.outlooks[0].covered_until if verdict.outlooks else now
        return Notification(
            title=words["dry_window"].format(label=label),
            body="\n".join([
                place,
                words["dry_until"].format(end=_clock(end, zone)),
                _footer(words, verdict, zone),
            ]),
            priority=2,
            tags=["sun_with_face"],
            click=user.click_url or None,
            actions=_leaving(user, words),
        )

    lead = verdict.leading
    lead_time = lead.onset - now
    imminent = rule.every is not None

    if imminent:
        title = (
            words["rain_now"]
            if lead_time <= timedelta(minutes=5)
            else words["rain_in"].format(delta=_span(words, lead_time))
        )
    else:
        title = words["rain_window"].format(label=label)

    span = (
        words["from_open"].format(start=_clock(lead.onset, zone))
        if lead.open_ended
        else words["from_until"].format(
            start=_clock(lead.onset, zone), end=_clock(lead.until, zone)
        )
    )
    totals = words["totals"].format(mm=lead.total_mm, peak=lead.peak_mm_per_hour)
    lines = [place, f"{span} · {totals}"]
    if lead.spells > 1:
        lines.append(words["more_spells"].format(count=lead.spells - 1))
    lines.append(_footer(words, verdict, zone))

    return Notification(
        title=title,
        body="\n".join(lines),
        priority=4 if imminent else 3,
        tags=["umbrella"],
        click=user.click_url or None,
        actions=_leaving(user, words),
    )


def describe(preset: str, at: str | None = None, language: str = "de") -> str:
    """One rule, in the words a person would use.

    The one place this wording lives: the same sentence goes into the test
    notification and onto the confirmation page, so they cannot drift apart.
    """
    words = texts(language)
    if preset == "imminent":
        return words["rule_imminent"]
    # A hand-written rule may leave the time implied; fall back to the preset's
    # own default rather than printing a question mark at somebody.
    at = at or (PRESETS.get(preset) or {}).get("at") or "?"
    key = f"rule_{preset}" if f"rule_{preset}" in words else "rule_daily"
    return words[key].format(at=at)


def test_notification(user) -> Notification:
    words = texts(user.language)
    rules = "\n".join(
        words["rule_bullet"].format(
            line=describe(
                rule.name,
                rule.at.strftime("%H:%M") if rule.at else None,
                user.language,
            )
        )
        for rule in user.rules
    )
    return Notification(
        title=words["test_title"],
        body=words["test_body"].format(rules=rules),
        priority=3,
        tags=["white_check_mark"],
        click=user.click_url or None,
    )


def _leaving(user, words: dict) -> list[dict]:
    """A way out, carried by every notification.

    ntfy never tells us that someone unsubscribed in the app, so a link they
    can use themselves is the only way anyone can actually leave.
    """
    url = getattr(user, "unsubscribe_url", "")
    if not url:
        return []
    return [{"action": "view", "label": words["unsubscribe"], "url": url, "clear": False}]


def _footer(words: dict, verdict: Consensus, zone: tzinfo) -> str:
    line = words["agreement"].format(agreeing=verdict.agreeing, answering=verdict.answering)
    # Never let a two-hour radar horizon read as a twelve-hour all-clear.
    furthest = max((o.covered_until for o in verdict.outlooks), default=None)
    if not verdict.will_rain and verdict.outlooks and all(o.truncated for o in verdict.outlooks):
        line += " · " + words["only_sees"].format(end=_clock(furthest, zone))
    return line


def _clock(moment: datetime, zone: tzinfo) -> str:
    return moment.astimezone(zone).strftime("%H:%M")


def _span(words: dict, span: timedelta) -> str:
    minutes = max(0, int(span.total_seconds() // 60))
    if minutes < 60:
        return words["minutes"].format(count=minutes)
    hours, minutes = divmod(minutes, 60)
    if minutes == 0:
        return words["hours_round"].format(hours=hours)
    return words["hours"].format(hours=hours, minutes=minutes)
