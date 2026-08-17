"""DWD radar nowcast (RV product) via Bright Sky.

Not a weather model, an extrapolation of what the radar composite is actually
seeing right now, at 5-minute resolution on a 1 km grid, two hours ahead. For
"it will rain in 40 minutes" this beats every forecast model in this package,
and it is worthless beyond its two-hour horizon.

Values are integers in units of 0.01 mm per 5 minutes, stamped with the end of
the interval. There is no probability, so the probability threshold does not
apply to this provider.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx

from ..models import Forecast, Location, Slot
from .base import get_json

URL = "https://api.brightsky.dev/radar"


class DWDRadarProvider:
    name = "dwd-radar"
    label = "DWD radar nowcast (RV)"

    def __init__(self, radius_m: int = 1500):
        self.radius_m = radius_m
        """Rain is reported if any pixel within this radius is wet. The nowcast
        drifts cells by a kilometre or two, so a point sample under-reports."""

    def fetch(self, client: httpx.Client, location: Location, until: datetime) -> Forecast:
        now = datetime.now(UTC)
        data = get_json(
            client,
            URL,
            {
                "lat": location.lat,
                "lon": location.lon,
                "distance": self.radius_m,
                "format": "plain",
            },
        )

        slots = []
        for frame in data.get("radar", []):
            grid = frame.get("precipitation_5") or []
            hundredths = max((max(row) for row in grid if row), default=0)
            end = datetime.fromisoformat(frame["timestamp"]).astimezone(UTC)
            slots.append(Slot(start=end - timedelta(minutes=5), end=end, mm=hundredths / 100))
        slots.sort(key=lambda s: s.start)

        # Bright Sky serves the latest RV run it has ingested, and that ingest
        # can lag by an hour. The run age is the honest measure of how much
        # nowcast is left, so carry it rather than assuming a full two hours.
        run_age = now - min((s.end for s in slots), default=now) + timedelta(minutes=5)
        return Forecast(
            provider=self.name,
            location=location,
            slots=slots,
            horizon_end=max((s.end for s in slots), default=now),
            source=(
                f"{self.label}, {self.radius_m} m radius, "
                f"run {int(run_age.total_seconds() // 60)} min old"
            ),
        )
