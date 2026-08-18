from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from gleichnass.rules import Rule, RuleState, build_rule
from gleichnass.util import parse_duration, parse_time_of_day

BERLIN = ZoneInfo("Europe/Berlin")
DEFAULTS = {
    "min_mm_per_hour": 0.2,
    "min_probability": 50.0,
    "min_total_mm": 0.1,
    "min_agreement": 1,
    "providers": ["icon-d2"],
}


def berlin(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 8, day, hour, minute, tzinfo=BERLIN).astimezone(UTC)


def digest(**overrides) -> Rule:
    return Rule(name="night", window=timedelta(hours=12), at=parse_time_of_day("20:00"), **overrides)


def watch(**overrides) -> Rule:
    return Rule(name="imminent", window=timedelta(hours=1), every=timedelta(minutes=15), **overrides)


def test_a_rule_needs_exactly_one_trigger():
    with pytest.raises(ValueError):
        Rule(name="x", window=timedelta(hours=1))
    with pytest.raises(ValueError):
        Rule(name="x", window=timedelta(hours=1), at=parse_time_of_day("20:00"),
             every=timedelta(minutes=15))


def test_digest_fires_at_its_hour_and_not_before():
    rule, fresh = digest(), RuleState()
    assert not rule.is_due(berlin(17, 19, 59), BERLIN, fresh)
    assert rule.is_due(berlin(17, 20, 0), BERLIN, fresh)
    assert rule.is_due(berlin(17, 20, 30), BERLIN, fresh)


def test_digest_gives_up_after_the_grace_window():
    """A machine asleep all evening must not deliver last night's forecast at 03:00."""
    rule = digest(grace=timedelta(hours=1))
    assert not rule.is_due(berlin(18, 3, 0), BERLIN, RuleState())


def test_digest_fires_once_a_day():
    rule = digest()
    state = RuleState(last_run_at=berlin(17, 20, 1))
    assert not rule.is_due(berlin(17, 20, 16), BERLIN, state)
    assert rule.is_due(berlin(18, 20, 1), BERLIN, state)


def test_digest_retries_within_grace_when_the_earlier_tick_did_not_reach_it():
    """last_run_at from before the trigger time does not count as today's run."""
    rule = digest()
    state = RuleState(last_run_at=berlin(17, 19, 45))
    assert rule.is_due(berlin(17, 20, 5), BERLIN, state)


def test_watch_rule_respects_its_interval():
    rule = watch()
    assert rule.is_due(berlin(17, 12, 0), BERLIN, RuleState())
    state = RuleState(last_run_at=berlin(17, 12, 0))
    assert not rule.is_due(berlin(17, 12, 10), BERLIN, state)
    assert rule.is_due(berlin(17, 12, 15), BERLIN, state)


def test_watch_rule_tolerates_an_early_cron_tick():
    rule = watch()
    state = RuleState(last_run_at=berlin(17, 12, 0))
    assert rule.is_due(berlin(17, 12, 14) + timedelta(seconds=45), BERLIN, state)


def test_the_same_shower_is_recognised():
    rule = watch(event_merge=timedelta(hours=1))
    state = RuleState(last_event_start=berlin(17, 14, 0))
    assert rule.is_same_event(berlin(17, 14, 20), state)
    assert not rule.is_same_event(berlin(17, 18, 0), state)
    assert not rule.is_same_event(berlin(17, 14, 0), RuleState())


def test_presets_expand_and_stay_overridable():
    assert build_rule({"preset": "night"}, DEFAULTS).at == parse_time_of_day("20:00")
    assert build_rule({"preset": "imminent"}, DEFAULTS).window == timedelta(hours=1)

    tweaked = build_rule({"preset": "imminent", "window": "2h", "min_mm_per_hour": 1.0}, DEFAULTS)
    assert tweaked.window == timedelta(hours=2)
    assert tweaked.threshold.min_mm_per_hour == 1.0
    assert tweaked.every == timedelta(minutes=15), "unset preset fields survive"


def test_no_preset_speaks_up_when_it_stays_dry():
    """Silence is the good news. Only a rule that asks for it hears about dry weather."""
    for preset in ("night", "morning", "imminent"):
        assert not build_rule({"preset": preset}, DEFAULTS).notify_when_dry
    assert build_rule({"preset": "night", "notify_when_dry": True}, DEFAULTS).notify_when_dry


def test_a_rule_can_be_written_from_scratch():
    rule = build_rule({"name": "commute", "at": "07:30", "window": "2h"}, DEFAULTS)
    assert (rule.name, rule.window) == ("commute", timedelta(hours=2))


def test_typos_in_a_rule_are_rejected_rather_than_ignored():
    with pytest.raises(ValueError, match="unknown keys"):
        build_rule({"preset": "night", "windwo": "3h"}, DEFAULTS)
    with pytest.raises(ValueError, match="preset"):
        build_rule({"window": "3h", "at": "07:00"}, DEFAULTS)


@pytest.mark.parametrize(
    ("text", "expected"),
    [("12h", timedelta(hours=12)), ("90m", timedelta(minutes=90)),
     ("2h30m", timedelta(hours=2, minutes=30)), ("15min", timedelta(minutes=15)),
     ("1d", timedelta(days=1))],
)
def test_duration_parsing(text, expected):
    assert parse_duration(text) == expected
