"""The one interface every weather source implements."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Protocol

import httpx

from ..models import Forecast, Location

USER_AGENT = "gleichnass/0.1 (https://github.com/ribalba/gleichnass)"


class Provider(Protocol):
    name: str
    label: str
    """Short human-readable description of the underlying model."""

    def fetch(self, client: httpx.Client, location: Location, until: datetime) -> Forecast:
        """Return precipitation slots from now up to (at least) `until`.

        A provider whose horizon is shorter than `until` returns what it has
        and sets `Forecast.horizon_end` accordingly; the caller reports the gap
        rather than silently treating "no data" as "no rain".
        """
        ...


def get_json(client: httpx.Client, url: str, params: dict, **kwargs) -> dict:
    response = client.get(url, params=params, headers={"User-Agent": USER_AGENT}, **kwargs)
    response.raise_for_status()
    return response.json()


def hour_containing(moment: datetime) -> datetime:
    """End of the (h-1, h] hour bucket that `moment` falls into."""
    on_the_hour = moment.replace(minute=0, second=0, microsecond=0)
    return on_the_hour if on_the_hour == moment else on_the_hour + timedelta(hours=1)
