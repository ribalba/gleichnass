"""MET Norway Locationforecast, a genuinely independent second opinion.

Everything else here traces back to the DWD, so when all providers agree they
may just be agreeing with themselves. MET runs its own model (ECMWF-driven
MEPS/HARMONIE), which makes disagreement informative.

Unlike the others, MET stamps an interval with its *start*: the entry at 14:00
carries `next_1_hours`, the rain between 14:00 and 15:00. Their terms of service
require an identifying User-Agent and coordinates truncated to four decimals.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx

from ..models import Forecast, Location, Slot
from .base import get_json

URL = "https://api.met.no/weatherapi/locationforecast/2.0/compact"


class MetNoProvider:
    name = "met-no"
    label = "MET Norway (Yr)"

    def fetch(self, client: httpx.Client, location: Location, until: datetime) -> Forecast:
        now = datetime.now(UTC)
        data = get_json(
            client,
            URL,
            {"lat": round(location.lat, 4), "lon": round(location.lon, 4)},
        )

        slots = []
        for entry in data.get("properties", {}).get("timeseries", []):
            details = entry.get("data", {}).get("next_1_hours", {}).get("details")
            if not details or details.get("precipitation_amount") is None:
                continue  # beyond ~2.5 days MET only offers 6-hour buckets
            start = datetime.fromisoformat(entry["time"]).astimezone(UTC)
            slots.append(
                Slot(
                    start=start,
                    end=start + timedelta(hours=1),
                    mm=float(details["precipitation_amount"]),
                    probability=_as_float(details.get("probability_of_precipitation")),
                )
            )
        slots.sort(key=lambda s: s.start)

        return Forecast(
            provider=self.name,
            location=location,
            slots=slots,
            horizon_end=max((s.end for s in slots), default=now),
            source=self.label,
        )


def _as_float(value) -> float | None:
    return None if value is None else float(value)
