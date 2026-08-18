from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from gleichnass.analyze import Outlook, consensus
from gleichnass import message as message_module
from gleichnass.message import render
from gleichnass.models import Location
from gleichnass.rules import build_rule

NOW = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)
DEFAULTS = {
    "min_mm_per_hour": 0.2, "min_probability": 50.0, "min_total_mm": 0.1,
    "min_agreement": 1, "providers": ["icon-d2"],
}


def person(language="de"):
    return SimpleNamespace(
        id="didi", name="Didi", language=language, zone=ZoneInfo("Europe/Berlin"),
        location=Location(47.66, 9.18, "Konstanz"), click_url="https://example.test/radar",
        rules=[build_rule({"preset": "night"}, DEFAULTS), build_rule({"preset": "imminent"}, DEFAULTS)],
    )


def outlook(*, rain=True, onset_in=1.0, until_in=2.0, window=12, truncated=False, **extra):
    window_end = NOW + timedelta(hours=window)
    result = Outlook(
        provider="icon-d2", source="test", window_start=NOW, window_end=window_end,
        covered_until=NOW + timedelta(hours=2) if truncated else window_end,
    )
    if rain:
        result.onset = NOW + timedelta(hours=onset_in)
        result.until = NOW + timedelta(hours=until_in)
        result.total_mm = extra.get("total_mm", 3.6)
        result.peak_mm_per_hour = extra.get("peak", 1.9)
        result.spells = extra.get("spells", 1)
    return result


def test_imminent_message_leads_with_the_lead_time():
    rule = build_rule({"preset": "imminent"}, DEFAULTS)
    verdict = consensus([outlook(onset_in=0.75, until_in=1.5, window=1)])
    note = render(person(), rule, verdict, NOW)

    assert note.title == "Regen in 45 Min"
    assert note.priority == 4 and note.tags == ["umbrella"]
    assert note.click == "https://example.test/radar"
    assert "Konstanz" in note.body


def test_rain_already_starting_is_not_announced_as_rain_in_0_min():
    rule = build_rule({"preset": "imminent"}, DEFAULTS)
    verdict = consensus([outlook(onset_in=0, until_in=0.5, window=1)])
    assert render(person(), rule, verdict, NOW).title == "Es fängt gleich an zu regnen"


def test_digest_names_the_window_rather_than_a_countdown():
    rule = build_rule({"preset": "night"}, DEFAULTS)
    note = render(person(), rule, consensus([outlook(spells=2)]), NOW)

    assert note.title == "Regen heute Nacht"
    assert note.priority == 3
    assert "15:00–16:00" in note.body  # 12:00 UTC is 14:00 in Berlin
    assert "danach noch 1 Schauer" in note.body


def test_a_shower_running_past_the_window_gets_an_open_ended_time():
    """Otherwise a 1-hour watch reports 'rain 12:45-13:00' purely because the
    window stopped there."""
    rule = build_rule({"preset": "imminent"}, DEFAULTS)
    verdict = consensus([outlook(onset_in=0.75, until_in=1.0, window=1)])
    assert "ab 14:45" in render(person(), rule, verdict, NOW).body


def test_the_all_clear_is_quiet_and_says_how_far_it_looked():
    rule = build_rule({"preset": "night"}, DEFAULTS)
    note = render(person(), rule, consensus([outlook(rain=False)]), NOW)

    assert note.title == "Kein Regen heute Nacht"
    assert note.priority == 2
    assert "Trocken bis 02:00" in note.body


def test_a_short_horizon_is_admitted_in_the_all_clear():
    """'No rain tonight' from a source that can only see two hours would be a lie."""
    rule = build_rule({"preset": "night"}, DEFAULTS)
    note = render(person(), rule, consensus([outlook(rain=False, truncated=True)]), NOW)
    assert "reicht nur bis 16:00" in note.body


def test_the_all_clear_quotes_the_provider_that_saw_furthest():
    """The radar is asked first but only reaches two hours; a digest must not
    tell somebody it stays dry until 14:00 when it means the whole day."""
    radar = outlook(rain=False, truncated=True)
    radar.provider = "dwd-radar"
    note = render(person(), build_rule({"preset": "night"}, DEFAULTS),
                  consensus([radar, outlook(rain=False)]), NOW)

    assert "Trocken bis 02:00" in note.body
    assert "reicht nur bis" not in note.body, "one full-length source is enough"


def test_english_works_too():
    rule = build_rule({"preset": "night"}, DEFAULTS)
    note = render(person("en"), rule, consensus([outlook()]), NOW)
    assert note.title == "Rain tonight"
    assert "peak" in note.body


@pytest.mark.parametrize("language", ["de", "en"])
def test_the_test_notification_lists_the_alerts_in_plain_words(language):
    """It lands on a lock screen, so no preset names and no intervals."""
    note = message_module.test_notification(person(language))

    assert "20:00" in note.body, "the time they chose is the useful part"
    for jargon in ("imminent", "preset:", "window", "15 Min", "alle 15"):
        assert jargon not in note.body


def test_the_watch_is_described_by_what_it_does_not_how_it_polls():
    assert message_module.describe("imminent", language="de") == "Immer wenn es gleich regnet"
    assert message_module.describe("imminent", language="en") == (
        "Whenever rain is about to start"
    )


def test_a_digest_names_its_own_time():
    assert message_module.describe("night", "21:30", "de") == "Jeden Abend um 21:30, für die Nacht"
    assert message_module.describe("morning", "06:45", "de") == "Jeden Morgen um 06:45, für den Tag"


def test_a_rule_without_an_explicit_time_falls_back_to_the_preset():
    assert message_module.describe("night", None, "de") == "Jeden Abend um 20:00, für die Nacht"


def test_a_hand_written_rule_still_gets_a_sentence():
    assert message_module.describe("bike-commute", "07:30", "de") == "Jeden Tag um 07:30"
