"""Open-Meteo, free, keyless, serves DWD ICON among other models.

Timestamps label the *end* of an interval: `precipitation` at 15:00 is the rain
that fell between 14:00 and 15:00. Verified against the 15-minute series, which
sums exactly onto the preceding hour.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import httpx

from ..models import Forecast, Location, Slot
from .base import get_json, hour_containing

URL = "https://api.open-meteo.com/v1/forecast"


class OpenMeteoProvider:
    def __init__(self, name: str = "open-meteo", model: str | None = None, label: str = ""):
        self.name = name
        self.model = model
        self.label = label or "Open-Meteo best-match"

    def fetch(self, client: httpx.Client, location: Location, until: datetime) -> Forecast:
        now = datetime.now(UTC)
        days = max(1, min(10, math.ceil((until - now).total_seconds() / 86400) + 1))
        params = {
            "latitude": location.lat,
            "longitude": location.lon,
            "hourly": "precipitation,precipitation_probability",
            "minutely_15": "precipitation",
            "timezone": "UTC",
            "forecast_days": days,
        }
        if self.model:
            params["models"] = self.model
        data = get_json(client, URL, params)

        hourly = _series(data.get("hourly", {}), "precipitation", "precipitation_probability")
        quarterly = _series(data.get("minutely_15", {}), "precipitation")

        # Prefer the 15-minute series where it exists, it is what makes the
        # "rain in the next hour" mode useful, and fall back to hourly beyond.
        probability_by_hour = {end: prob for end, _, prob in hourly}
        slots = [
            Slot(end - timedelta(minutes=15), end, mm, probability_by_hour.get(hour_containing(end)))
            for end, mm, _ in quarterly
        ]
        fine_until = max((s.end for s in slots), default=now)
        slots += [
            Slot(end - timedelta(hours=1), end, mm, prob)
            for end, mm, prob in hourly
            if end - timedelta(hours=1) >= fine_until
        ]
        slots.sort(key=lambda s: s.start)

        return Forecast(
            provider=self.name,
            location=location,
            slots=slots,
            horizon_end=max((s.end for s in slots), default=now),
            source=self.label,
        )


def _series(block: dict, value_key: str, probability_key: str | None = None):
    """Yield (end, mm, probability) triples, skipping gaps in the model output."""
    times = block.get("time") or []
    values = block.get(value_key) or []
    probabilities = block.get(probability_key) if probability_key else None
    rows = []
    for index, stamp in enumerate(times):
        mm = values[index] if index < len(values) else None
        if mm is None:
            continue
        probability = None
        if probabilities and index < len(probabilities):
            probability = probabilities[index]
        rows.append((datetime.fromisoformat(stamp).replace(tzinfo=UTC), float(mm), probability))
    return rows
