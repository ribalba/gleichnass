"""Registry of weather sources. Adding one is a single entry here."""

from __future__ import annotations

from .base import Provider
from .brightsky import BrightSkyProvider
from .dwd_radar import DWDRadarProvider
from .met_no import MetNoProvider
from .open_meteo import OpenMeteoProvider

_FACTORIES = {
    "dwd-radar": DWDRadarProvider,
    "icon-d2": lambda: OpenMeteoProvider(
        name="icon-d2", model="icon_d2", label="DWD ICON-D2 (2 km) via Open-Meteo"
    ),
    "brightsky": BrightSkyProvider,
    "open-meteo": OpenMeteoProvider,
    "met-no": MetNoProvider,
}

# Ordered shortest-range-first, which is also roughly most-accurate-first.
ALL = list(_FACTORIES)
DEFAULT = ["dwd-radar", "icon-d2", "brightsky", "met-no"]


def build(name: str) -> Provider:
    try:
        return _FACTORIES[name]()
    except KeyError:
        raise ValueError(f"unknown provider {name!r}; available: {', '.join(ALL)}") from None


__all__ = ["ALL", "DEFAULT", "Provider", "build"]
