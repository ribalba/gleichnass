"""Turn a forecast plus a time window into a yes/no rain answer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .models import Forecast, Slot


@dataclass(frozen=True)
class Threshold:
    min_mm_per_hour: float = 0.2
    min_probability: float = 50.0
    """Ignored for providers that report no probability (radar, MET Norway)."""
    min_total_mm: float = 0.1
    """A spell must add up to this much before it counts. Without it a single
    noisy radar frame, 0.04 mm over five minutes, clears the intensity
    threshold and buzzes a phone about nothing."""

    def hits(self, slot: Slot) -> bool:
        if slot.mm_per_hour < self.min_mm_per_hour:
            return False
        if slot.probability is not None and slot.probability < self.min_probability:
            return False
        return True


@dataclass
class Outlook:
    provider: str
    source: str
    window_start: datetime
    window_end: datetime
    covered_until: datetime
    """How far into the window this provider could actually see."""
    onset: datetime | None = None
    until: datetime | None = None
    """Start and end of the *first* rain spell, not of the whole wet period, a notification should say when it starts, not lump two showers and the dry
    afternoon between them into one span."""
    spells: int = 0
    """Number of separate rain spells in the window."""
    total_mm: float = 0.0
    """Summed over the whole window, across all spells."""
    peak_mm_per_hour: float = 0.0
    max_probability: float | None = None

    @property
    def will_rain(self) -> bool:
        return self.onset is not None

    @property
    def has_data(self) -> bool:
        return self.covered_until > self.window_start

    @property
    def truncated(self) -> bool:
        """True when the provider's horizon ends before the window does."""
        return self.has_data and self.covered_until < self.window_end

    @property
    def open_ended(self) -> bool:
        """The first spell was still going when the window ran out, so its end
        time is an artefact of the window rather than a forecast."""
        return self.until is not None and self.until >= self.window_end


def analyze(
    forecast: Forecast,
    window_start: datetime,
    window_end: datetime,
    threshold: Threshold = Threshold(),
) -> Outlook:
    slots = forecast.between(window_start, window_end)
    covered_until = min(forecast.horizon_end, window_end)
    if slots:
        covered_until = min(max(s.end for s in slots), window_end)

    outlook = Outlook(
        provider=forecast.provider,
        source=forecast.source,
        window_start=window_start,
        window_end=window_end,
        covered_until=max(covered_until, window_start),
    )

    hits = [s for s in slots if threshold.hits(s)]
    if not hits:
        return outlook

    spells = [s for s in _spells(hits) if sum(x.mm for x in s) >= threshold.min_total_mm]
    if not spells:
        return outlook

    hits = [slot for spell in spells for slot in spell]
    first = spells[0]
    outlook.spells = len(spells)
    outlook.onset = max(first[0].start, window_start)
    outlook.until = min(first[-1].end, window_end)
    outlook.total_mm = sum(s.mm for s in hits)
    outlook.peak_mm_per_hour = max(s.mm_per_hour for s in hits)
    probabilities = [s.probability for s in hits if s.probability is not None]
    outlook.max_probability = max(probabilities) if probabilities else None
    return outlook


@dataclass
class Consensus:
    """What several providers, taken together, say about one window."""

    outlooks: list[Outlook]
    """Only those that had data, in the order the providers were configured."""
    leading: Outlook | None = None
    """The one the notification quotes. Providers are configured most-trustworthy
    first, so this is the first that expects rain, but one that can see the
    whole window wins over one that cannot, otherwise a two-hour radar blip ends
    up being announced as the forecast for the whole night."""
    agreeing: int = 0
    answering: int = 0
    will_rain: bool = False


def consensus(outlooks: list[Outlook], min_agreement: int = 1) -> Consensus:
    answered = [o for o in outlooks if o.has_data]
    wet = [o for o in answered if o.will_rain]
    ranked = [o for o in wet if not o.truncated] + [o for o in wet if o.truncated]
    return Consensus(
        outlooks=answered,
        leading=next(iter(ranked), None) or next(iter(answered), None),
        agreeing=len(wet),
        answering=len(answered),
        will_rain=len(wet) >= max(1, min_agreement),
    )


def _spells(hits: list[Slot]) -> list[list[Slot]]:
    """Group consecutive wet slots, splitting wherever a dry slot intervenes."""
    groups: list[list[Slot]] = [[hits[0]]]
    for slot in hits[1:]:
        if slot.start > groups[-1][-1].end:
            groups.append([slot])
        else:
            groups[-1].append(slot)
    return groups
