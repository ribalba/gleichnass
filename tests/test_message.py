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


def test_english_works_too():
    rule = build_rule({"preset": "night"}, DEFAULTS)
    note = render(person("en"), rule, consensus([outlook()]), NOW)
    assert note.title == "Rain tonight"
    assert "peak" in note.body


@pytest.mark.parametrize("language", ["de", "en"])
def test_the_test_notification_explains_what_was_signed_up_for(language):
    note = message_module.test_notification(person(language))
    assert "night" in note.body and "imminent" in note.body
    assert "20:00" in note.body
    assert ":00" not in note.body.split("night")[1].split("\n")[0].replace("20:00", "")
