"""Turn "Konstanz" into coordinates, so nobody has to look up their latitude."""

from __future__ import annotations

import httpx

from .models import Location
from .providers.base import get_json

URL = "https://geocoding-api.open-meteo.com/v1/search"


def search(
    client: httpx.Client, place: str, country: str = "DE", limit: int = 6
) -> list[Location]:
    """Candidate places for a partial name, best match first.

    Results from `country` come first rather than being the only ones kept: a
    German service should put Konstanz above Constanza without pretending
    nowhere else exists.
    """
    place = (place or "").strip()
    if len(place) < 2:
        return []
    data = get_json(
        client, URL, {"name": place, "count": 10, "language": "de", "format": "json"}
    )
    results = data.get("results") or []
    ordered = [r for r in results if r.get("country_code") == country]
    ordered += [r for r in results if r.get("country_code") != country]

    found = []
    for entry in ordered[:limit]:
        label = ", ".join(
            part for part in
            (entry.get("name"), entry.get("admin1"), entry.get("country_code")) if part
        )
        found.append(Location(lat=entry["latitude"], lon=entry["longitude"], name=label))
    return found


def lookup(client: httpx.Client, place: str, country: str = "DE") -> Location:
    matches = search(client, place, country, limit=1)
    if not matches:
        raise LookupError(f"no place found for {place!r}")
    return matches[0]
