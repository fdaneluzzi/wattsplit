"""Find recurring on/off pairs among edges no device explained.

This is the "inbox" behind the UI: it turns unexplained steps into candidate
appliances ("a ~4.4 kW load, 3 times this week, ~8 min each") that the user can
name, which then become latent devices.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from statistics import median

from .clock import day_of, hour_of_day
from .models import Edge


@dataclass(frozen=True, slots=True)
class Candidate:
    level_w: float
    count: int
    median_duration_s: float
    typical_hour: int  # local hour, approximate (median; ignores midnight wrap)
    days: int = 0  # on how many different days it happened
    hours: tuple[int, ...] = ()  # the distinct local hours it happened in
    duration_p10_s: float = 0.0
    duration_p90_s: float = 0.0
    starts: tuple[float, ...] = ()  # when each occurrence began
    likely_device: str | None = None  # a state device that is on suspiciously often then
    lift: float = 0.0  # how much more often than by chance
    share_on: float = 0.0  # share of occurrences during which that device was on

    @property
    def concentrated(self) -> bool:
        """Happens at a few hours of the day (a shower), not all day (a pump)."""
        return len(self.hours) <= 4

    @property
    def coherent(self) -> bool:
        """Its runs last about the same; a very wide spread means several sources."""
        return self.duration_p90_s <= 25.0 * max(self.duration_p10_s, 30.0)


@dataclass(frozen=True, slots=True)
class _Pair:
    level_w: float
    start_ts: float
    duration_s: float
    rise: Edge
    fall: Edge


class Discoverer:
    """Pairs unexplained rises with matching falls and clusters them by level."""

    def __init__(
        self,
        tolerance: float = 0.10,
        max_duration_s: float = 4 * 3600,
        max_edges: int = 5000,
    ) -> None:
        self._tol = tolerance
        self._max_duration_s = max_duration_s
        self._edges: deque[Edge] = deque(maxlen=max_edges)

    def add(self, edge: Edge) -> None:
        self._edges.append(edge)

    def _pairs(self) -> list[_Pair]:
        """Match each fall with the most recent unmatched rise of the same size.

        Most recent, not oldest: when a fall is lost, the load's next rise and fall
        should still pair with each other. Pairing the old rise with the later fall
        invented runs of hours (and left the real run unpaired).
        """
        edges = sorted(self._edges, key=lambda e: e.ts)
        open_rises: list[Edge] = []
        pairs = []
        for edge in edges:
            if edge.delta_w > 0:
                open_rises.append(edge)
                continue
            open_rises = [r for r in open_rises if edge.ts - r.ts <= self._max_duration_s]
            for k in range(len(open_rises) - 1, -1, -1):
                rise = open_rises[k]
                if abs(-edge.delta_w - rise.delta_w) <= self._tol * rise.delta_w:
                    del open_rises[k]
                    pairs.append(
                        _Pair(
                            (rise.delta_w - edge.delta_w) / 2,
                            rise.ts,
                            edge.ts - rise.ts,
                            rise,
                            edge,
                        )
                    )
                    break
        return pairs

    def _clusters(self) -> list[list[_Pair]]:
        clusters: list[list[_Pair]] = []
        for pair in sorted(self._pairs(), key=lambda p: p.level_w):
            if clusters:
                ref = median(p.level_w for p in clusters[-1])
                if pair.level_w - ref <= self._tol * ref:
                    clusters[-1].append(pair)
                    continue
            clusters.append([pair])
        return clusters

    # Within a group, runs this many times shorter or longer than the typical one are
    # different things (a 6-minute pump cycle is not a 3-hour heating program).
    _DURATION_BAND = 4.0

    def _split(self, pairs: list[_Pair], depth: int = 0) -> list[list[_Pair]]:
        """The core of runs lasting about as long as the typical one, then the
        shorter and longer rest, each split again in the same way."""
        if len(pairs) < 2 or depth >= 3:
            return [pairs]
        typical = median(p.duration_s for p in pairs)
        low, high = typical / self._DURATION_BAND, typical * self._DURATION_BAND
        core = [p for p in pairs if low <= p.duration_s <= high]
        shorter = [p for p in pairs if p.duration_s < low]
        longer = [p for p in pairs if p.duration_s > high]
        if not shorter and not longer:
            return [core]
        groups = [core] if core else []
        for rest in (shorter, longer):
            if rest:
                groups.extend(self._split(rest, depth + 1))
        return groups

    def _groups(self) -> list[list[_Pair]]:
        """Pairs of similar power, split further by how long they last.

        Grouping by power alone merged everything that happens to draw about the
        same (a pump, a washer's heater, a compressor), and a rise whose true fall
        was lost got paired with someone else's fall, inventing runs of hours.
        """
        return [g for cluster in self._clusters() for g in self._split(cluster)]

    @staticmethod
    def _percentile(values: list[float], q: float) -> float:
        ordered = sorted(values)
        return ordered[min(len(ordered) - 1, int(q * len(ordered)))]

    def candidates(self, min_count: int = 2) -> list[Candidate]:
        out = []
        for group in self._groups():
            if len(group) < min_count:
                continue
            starts = sorted(p.start_ts for p in group)
            durations = [p.duration_s for p in group]
            out.append(
                Candidate(
                    level_w=median(p.level_w for p in group),
                    count=len(group),
                    median_duration_s=median(durations),
                    typical_hour=int(median(hour_of_day(t) for t in starts)),
                    days=len({day_of(t) for t in starts}),
                    hours=tuple(sorted({hour_of_day(t) for t in starts})),
                    duration_p10_s=self._percentile(durations, 0.1),
                    duration_p90_s=self._percentile(durations, 0.9),
                    starts=tuple(starts),
                )
            )
        return sorted(out, key=lambda c: c.count, reverse=True)

    def consume(self, level_w: float) -> None:
        """Drop the edges of every pair near ``level_w`` (after assignment)."""
        drop: set[Edge] = set()
        for pair in self._pairs():
            if abs(pair.level_w - level_w) <= self._tol * level_w:
                drop.update((pair.rise, pair.fall))
        self._edges = deque(
            (e for e in self._edges if e not in drop), maxlen=self._edges.maxlen
        )
