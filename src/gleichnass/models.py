"""Provider-independent representation of a precipitation forecast."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class Location:
    lat: float
    lon: float
    name: str | None = None

    def __str__(self) -> str:
        coords = f"{self.lat:.4f}, {self.lon:.4f}"
        return f"{self.name} ({coords})" if self.name else coords


@dataclass(frozen=True)
class Slot:
    """Precipitation falling in the half-open interval (start, end].

    Providers disagree on how they label a precipitation interval: Open-Meteo,
    Bright Sky and the DWD radar stamp a value with the *end* of its interval,
    MET Norway with the *start*. Each provider normalises to explicit
    start/end here so nothing downstream has to know.
    """

    start: datetime
    end: datetime
    mm: float
    probability: float | None = None

    @property
    def mm_per_hour(self) -> float:
        hours = (self.end - self.start).total_seconds() / 3600
        return self.mm / hours if hours > 0 else 0.0

    def overlaps(self, start: datetime, end: datetime) -> bool:
        return self.start < end and self.end > start


@dataclass
class Forecast:
    provider: str
    location: Location
    slots: list[Slot]
    horizon_end: datetime
    """Last moment this forecast makes a statement about."""
    source: str = ""
    """Human-readable data origin, e.g. 'DWD ICON-D2'."""

    @property
    def resolution(self) -> timedelta | None:
        return self.slots[0].end - self.slots[0].start if self.slots else None

    def between(self, start: datetime, end: datetime) -> list[Slot]:
        return [s for s in self.slots if s.overlaps(start, end)]
