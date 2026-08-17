"""Each provider labels precipitation intervals differently. These fixtures are
trimmed real responses; getting the convention wrong shifts rain by an hour.
"""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from gleichnass.models import Location
from gleichnass.providers.brightsky import BrightSkyProvider
from gleichnass.providers.dwd_radar import DWDRadarProvider
from gleichnass.providers.met_no import MetNoProvider
from gleichnass.providers.open_meteo import OpenMeteoProvider

MUNICH = Location(48.14, 11.58)
UNTIL = datetime.now(UTC) + timedelta(hours=6)


def client_returning(payload):
    return httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)))


def test_open_meteo_stamps_the_end_of_the_interval():
    """hourly[15:00] is the rain from 14:00 to 15:00, and the 15-minute series
    sums exactly onto it, verified against the live API."""
    payload = {
        "hourly": {
            "time": ["2026-08-17T14:00", "2026-08-17T15:00"],
            "precipitation": [0.0, 1.9],
            "precipitation_probability": [40, 80],
        },
        "minutely_15": {
            "time": [
                "2026-08-17T14:15",
                "2026-08-17T14:30",
                "2026-08-17T14:45",
                "2026-08-17T15:00",
            ],
            "precipitation": [0.5, 0.7, 0.1, 0.6],
        },
    }
    forecast = OpenMeteoProvider().fetch(client_returning(payload), MUNICH, UNTIL)

    first = forecast.slots[0]
    assert first.start == datetime(2026, 8, 17, 14, 0, tzinfo=UTC)
    assert first.end == datetime(2026, 8, 17, 14, 15, tzinfo=UTC)
    assert first.mm == 0.5
    # Probability comes from the hour that contains the quarter, i.e. 15:00.
    assert all(slot.probability == 80 for slot in forecast.slots)
    assert sum(slot.mm for slot in forecast.slots) == pytest.approx(1.9)


def test_open_meteo_does_not_double_count_across_resolutions():
    payload = {
        "hourly": {
            "time": ["2026-08-17T14:00", "2026-08-17T15:00", "2026-08-17T16:00"],
            "precipitation": [1.0, 2.0, 3.0],
            "precipitation_probability": [50, 50, 50],
        },
        "minutely_15": {
            "time": ["2026-08-17T14:15", "2026-08-17T14:30"],
            "precipitation": [0.5, 0.5],
        },
    }
    forecast = OpenMeteoProvider().fetch(client_returning(payload), MUNICH, UNTIL)
    # The 15-minute data covers 14:00-14:30, so the hour ending 15:00 must be
    # dropped rather than added on top of it; 16:00 survives.
    assert sum(slot.mm for slot in forecast.slots) == pytest.approx(1.0 + 3.0)


def test_brightsky_stamps_the_end_of_the_interval():
    payload = {
        "weather": [
            {
                "timestamp": "2026-08-19T14:00:00+02:00",
                "precipitation": 0.5,
                "precipitation_probability": 57,
            }
        ],
        "sources": [{"station_name": "MUENCHEN-STADT"}],
    }
    forecast = BrightSkyProvider().fetch(client_returning(payload), MUNICH, UNTIL)

    slot = forecast.slots[0]
    assert slot.start == datetime(2026, 8, 19, 11, 0, tzinfo=UTC)
    assert slot.end == datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
    assert slot.probability == 57
    assert "MUENCHEN-STADT" in forecast.source


def test_met_no_stamps_the_start_of_the_interval():
    payload = {
        "properties": {
            "timeseries": [
                {
                    "time": "2026-08-17T10:00:00Z",
                    "data": {"next_1_hours": {"details": {"precipitation_amount": 4.4}}},
                },
                {"time": "2026-08-17T11:00:00Z", "data": {"next_6_hours": {"details": {}}}},
            ]
        }
    }
    forecast = MetNoProvider().fetch(client_returning(payload), MUNICH, UNTIL)

    assert len(forecast.slots) == 1, "entries without next_1_hours are skipped"
    slot = forecast.slots[0]
    assert slot.start == datetime(2026, 8, 17, 10, 0, tzinfo=UTC)
    assert slot.end == datetime(2026, 8, 17, 11, 0, tzinfo=UTC)
    assert slot.probability is None


def test_radar_converts_hundredths_and_takes_the_wettest_nearby_pixel():
    payload = {
        "radar": [
            {
                "timestamp": "2026-08-17T09:25:00+00:00",
                "precipitation_5": [[0, 45], [3, 0]],
            }
        ],
        "latlon_position": {"x": 0.5, "y": 0.5},
    }
    forecast = DWDRadarProvider(radius_m=1000).fetch(client_returning(payload), MUNICH, UNTIL)

    slot = forecast.slots[0]
    assert slot.start == datetime(2026, 8, 17, 9, 20, tzinfo=UTC)
    assert slot.end == datetime(2026, 8, 17, 9, 25, tzinfo=UTC)
    assert slot.mm == 0.45
    assert slot.mm_per_hour == pytest.approx(5.4)
