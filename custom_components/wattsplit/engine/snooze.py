"""Loads the user chose to skip: asked again the next time they show up."""

from __future__ import annotations

from dataclasses import dataclass

from .discovery import Candidate


@dataclass(slots=True)
class Snoozed:
    level_w: float
    duration_s: float  # typical run length when skipped
    at: float  # when it was skipped


class Snoozes:
    """Skipped recurring loads.

    "I do not know what this is, ask me again" is not "never ask": the load stays
    out of the inbox until it runs again after the moment it was skipped, and then
    comes back with the new run in its list, so the user can match it with what
    they were doing. A skip is forgotten after 60 days. Permanent dismissal is
    Home Assistant's own "Ignore" on the Repairs item.
    """

    EXPIRY_S = 60 * 86_400.0
    _LEVEL_TOLERANCE = 0.10  # same as the discovery inbox: one level
    _DURATION_FACTOR = 4.0  # same family of run length

    def __init__(self, records: list[Snoozed] | None = None) -> None:
        self._records: list[Snoozed] = list(records or [])
        self.changed = False  # a record was added or dropped since the last save

    def add(self, level_w: float, duration_s: float, at: float) -> None:
        self._records = [r for r in self._records if not self._same(r, level_w, duration_s)]
        self._records.append(Snoozed(level_w, duration_s, at))
        self.changed = True

    def hides(self, candidate: Candidate, now: float) -> bool:
        """True while the candidate is skipped; a new run, or time, ends the skip."""
        for record in list(self._records):
            if now - record.at > self.EXPIRY_S:
                self._drop(record)
                continue
            if not self._same(record, candidate.level_w, candidate.median_duration_s):
                continue
            if candidate.starts and max(candidate.starts) > record.at:
                self._drop(record)
                return False
            return True
        return False

    def visible(self, candidates: list[Candidate], now: float) -> list[Candidate]:
        return [c for c in candidates if not self.hides(c, now)]

    def as_list(self) -> list[dict[str, float]]:
        return [
            {"level_w": r.level_w, "duration_s": r.duration_s, "at": r.at}
            for r in self._records
        ]

    @classmethod
    def from_list(cls, items: list[dict[str, float]] | None) -> Snoozes:
        records = []
        for item in items or []:
            try:
                records.append(
                    Snoozed(float(item["level_w"]), float(item["duration_s"]), float(item["at"]))
                )
            except (KeyError, TypeError, ValueError):
                continue  # a damaged record is not worth failing the setup
        return cls(records)

    def _same(self, record: Snoozed, level_w: float, duration_s: float) -> bool:
        if abs(level_w - record.level_w) > self._LEVEL_TOLERANCE * record.level_w:
            return False
        low, high = sorted((max(duration_s, 1.0), max(record.duration_s, 1.0)))
        return high / low <= self._DURATION_FACTOR

    def _drop(self, record: Snoozed) -> None:
        self._records.remove(record)
        self.changed = True
