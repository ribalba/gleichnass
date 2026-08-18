from datetime import UTC, datetime, timedelta

import pytest

from gleichnass.analyze import Outlook, Threshold, analyze, consensus
from gleichnass.models import Forecast, Location, Slot

BERLIN = Location(52.52, 13.40, "Berlin")
T0 = datetime(2026, 8, 17, 12, 0, tzinfo=UTC)


def hourly(*mm_values, probability=100.0, start=T0):
    slots = [
        Slot(start + timedelta(hours=i), start + timedelta(hours=i + 1), mm, probability)
        for i, mm in enumerate(mm_values)
    ]
    return Forecast("test", BERLIN, slots, horizon_end=slots[-1].end)


def test_dry_window():
    outlook = analyze(hourly(0.0, 0.05, 0.0), T0, T0 + timedelta(hours=3))
    assert not outlook.will_rain
    assert outlook.has_data


def test_onset_is_the_start_of_the_first_wet_slot():
    outlook = analyze(hourly(0.0, 1.0, 1.0), T0, T0 + timedelta(hours=3))
    assert outlook.onset == T0 + timedelta(hours=1)
    assert outlook.until == T0 + timedelta(hours=3)
    assert outlook.total_mm == 2.0


def test_spells_split_on_a_dry_gap():
    outlook = analyze(hourly(1.0, 0.0, 2.0), T0, T0 + timedelta(hours=3))
    assert outlook.spells == 2
    # The reported span is the first shower only, not the whole afternoon.
    assert (outlook.onset, outlook.until) == (T0, T0 + timedelta(hours=1))
    assert outlook.total_mm == 3.0


def test_low_probability_suppresses_a_wet_slot():
    forecast = hourly(2.0, probability=30.0)
    assert not analyze(forecast, T0, T0 + timedelta(hours=1)).will_rain
    loose = Threshold(min_probability=20.0)
    assert analyze(forecast, T0, T0 + timedelta(hours=1), loose).will_rain


def test_missing_probability_falls_back_to_the_intensity_threshold():
    """Radar and MET Norway report no probability; they must not be filtered out."""
    forecast = hourly(2.0, probability=None)
    assert analyze(forecast, T0, T0 + timedelta(hours=1)).will_rain


def test_short_horizon_is_reported_not_treated_as_dry():
    forecast = hourly(0.0, 0.0)  # two hours of data
    outlook = analyze(forecast, T0, T0 + timedelta(hours=12))
    assert not outlook.will_rain
    assert outlook.truncated
    assert outlook.covered_until == T0 + timedelta(hours=2)


def test_no_overlapping_data_means_no_data():
    forecast = hourly(5.0, start=T0 - timedelta(hours=6))
    outlook = analyze(forecast, T0, T0 + timedelta(hours=3))
    assert not outlook.has_data
    assert not outlook.will_rain


def test_window_clips_a_shower_that_started_before_it():
    forecast = hourly(1.0, 1.0, start=T0 - timedelta(hours=1))
    outlook = analyze(forecast, T0, T0 + timedelta(hours=1))
    assert outlook.onset == T0
    assert outlook.until == T0 + timedelta(hours=1)


def test_intensity_scales_with_slot_length():
    """0.1 mm in five minutes is heavy rain; in an hour it is drizzle."""
    burst = Slot(T0, T0 + timedelta(minutes=5), 0.1)
    drizzle = Slot(T0, T0 + timedelta(hours=1), 0.1)
    assert burst.mm_per_hour == pytest.approx(1.2)
    assert drizzle.mm_per_hour == pytest.approx(0.1)
    threshold = Threshold(min_mm_per_hour=0.2, min_probability=0)
    assert threshold.hits(burst)
    assert not threshold.hits(drizzle)


def _outlook(name, *, hours, rain=False):
    start = datetime(2026, 8, 17, 20, 0, tzinfo=UTC)
    result = Outlook(provider=name, source=name, window_start=start,
                     window_end=start + timedelta(hours=12),
                     covered_until=start + timedelta(hours=hours))
    if rain:
        result.onset = start + timedelta(hours=1)
        result.until = start + timedelta(hours=2)
    return result


def test_an_all_clear_is_led_by_the_provider_that_saw_furthest():
    short, long = _outlook("dwd-radar", hours=2), _outlook("icon-d2", hours=12)
    assert consensus([short, long]).leading is long
    assert consensus([short]).leading is short, "a short horizon still answers"


def test_rain_still_outranks_a_longer_dry_forecast():
    """Reach only decides the all-clear. A wet provider is still what gets quoted."""
    wet = _outlook("dwd-radar", hours=2, rain=True)
    verdict = consensus([wet, _outlook("icon-d2", hours=12)])
    assert verdict.leading is wet and verdict.will_rain
