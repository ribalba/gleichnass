"""Bright Sky, free, keyless JSON layer over the DWD's own open data.

Germany only. Serves DWD MOSMIX station forecasts, which is as close to "what
the Deutscher Wetterdienst officially thinks" as you can get without parsing
KML off their FTP server. Timestamps label the end of the interval:
"Total precipitation during previous 60 minutes".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx

from ..models import Forecast, Location, Slot
from .base import get_json

URL = "https://api.brightsky.dev/weather"


class BrightSkyProvider:
    name = "brightsky"
    label = "DWD MOSMIX via Bright Sky"

    def fetch(self, client: httpx.Client, location: Location, until: datetime) -> Forecast:
        now = datetime.now(UTC)
        data = get_json(
            client,
            URL,
            {
                "lat": location.lat,
                "lon": location.lon,
                "date": now.date().isoformat(),
                "last_date": (until + timedelta(days=1)).date().isoformat(),
                "tz": "UTC",
            },
        )

        slots = []
        for entry in data.get("weather", []):
            mm = entry.get("precipitation")
            if mm is None:
                continue
            end = datetime.fromisoformat(entry["timestamp"]).astimezone(UTC)
            slots.append(
                Slot(
                    start=end - timedelta(hours=1),
                    end=end,
                    mm=float(mm),
                    probability=_as_float(entry.get("precipitation_probability")),
                )
            )
        slots.sort(key=lambda s: s.start)

        station = next(
            (s.get("station_name") for s in data.get("sources", []) if s.get("station_name")),
            None,
        )
        return Forecast(
            provider=self.name,
            location=location,
            slots=slots,
            horizon_end=max((s.end for s in slots), default=now),
            source=f"{self.label} ({station})" if station else self.label,
        )


def _as_float(value) -> float | None:
    return None if value is None else float(value)
