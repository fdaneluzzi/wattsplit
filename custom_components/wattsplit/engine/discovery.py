"""Find recurring on/off pairs among edges no device explained.

This is the "inbox" behind the UI: it turns unexplained steps into candidate
appliances ("a ~4.4 kW load, 3 times this week, ~8 min each") that the user can
name, which then become latent devices.
"""

from __future__ import annotations

from bisect import bisect_right
from collections import deque
from dataclasses import dataclass
from statistics import median

from .clock import day_of, hour_of_day
from .models import Edge

# Run-length buckets of a candidate: <1 min, 1-5, 5-15, 15-60, 1-3 h, over 3 h.
DURATION_EDGES_S = (60.0, 300.0, 900.0, 3600.0, 10800.0)
RECENT_OCCURRENCES = 5


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
    # the latest occurrences, newest first: (start_ts, duration_s, level_w)
    recent: tuple[tuple[float, float, float], ...] = ()
    hour_counts: tuple[int, ...] = ()  # occurrences starting in each local hour (24)
    duration_counts: tuple[int, ...] = ()  # runs per DURATION_EDGES_S bucket (6)
    # share of the runs that overlap another run of the same group: one appliance
    # cannot be on twice at once, so these are several sources or a wrong pairing
    overlap_share: float = 0.0
    likely_device: str | None = None  # a state device that is on suspiciously often then
    lift: float = 0.0  # how much more often than by chance
    share_on: float = 0.0  # share of occurrences during which that device was on

    MAX_OVERLAP_SHARE = 0.2

    @property
    def concentrated(self) -> bool:
        """Happens at a few hours of the day (a shower), not all day (a pump)."""
        return len(self.hours) <= 4

    @property
    def coherent(self) -> bool:
        """Looks like one source: runs last about the same and do not overlap.

        A very wide spread of lengths means several sources. So does overlap: the
        same appliance cannot start again while it is still running. A stray overlap
        or two (a mispaired run) is tolerated in a group with many runs.
        """
        return (
            self.duration_p90_s <= 25.0 * max(self.duration_p10_s, 30.0)
            and self.overlap_share <= self.MAX_OVERLAP_SHARE
        )


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

    # How different the phases of a rise and of its fall may be (0 = the same share on
    # every channel, 1 = no channel in common). Real runs of one appliance rise and
    # fall on the same channels: on one house, short pump runs had a median of 0.00 and
    # 95 % were under 0.2, while 40 % of the pairs found by size alone were above it.
    _MAX_FOOTPRINT_DISTANCE = 0.3
    _FOOTPRINT_TIE = 0.1  # closer than this counts as equally good: take the most recent

    @staticmethod
    def _footprint_distance(rise: Edge, fall: Edge) -> float | None:
        """Total-variation distance between the channel shares of two steps.

        ``None`` when either has no per-channel breakdown (nothing to compare).
        """
        a = {c: abs(p) for c, p in rise.parts}
        b = {c: abs(p) for c, p in fall.parts}
        total_a, total_b = sum(a.values()), sum(b.values())
        if not total_a or not total_b:
            return None
        return 0.5 * sum(
            abs(a.get(c, 0.0) / total_a - b.get(c, 0.0) / total_b) for c in a.keys() | b.keys()
        )

    def _pairs(self) -> list[_Pair]:
        """Match each fall with an unmatched rise of the same size and the same phases.

        Among the rises of the right size the one whose channels look most like the
        fall wins, and the most recent when they are about equally good: when a fall
        is lost, the load's next rise and fall should still pair with each other, and
        pairing the old rise with the later fall invented runs of hours. A rise and a
        fall that share almost no channels are two different loads and are not paired,
        however close their sizes.
        """
        edges = sorted(self._edges, key=lambda e: e.ts)
        open_rises: list[Edge] = []
        pairs = []
        for edge in edges:
            if edge.delta_w > 0:
                open_rises.append(edge)
                continue
            open_rises = [r for r in open_rises if edge.ts - r.ts <= self._max_duration_s]
            best: tuple[int, float] | None = None
            for k in range(len(open_rises) - 1, -1, -1):
                rise = open_rises[k]
                if abs(-edge.delta_w - rise.delta_w) > self._tol * rise.delta_w:
                    continue
                distance = self._footprint_distance(rise, edge)
                if distance is not None and distance > self._MAX_FOOTPRINT_DISTANCE:
                    continue
                score = distance or 0.0
                if best is None or score < best[1] - self._FOOTPRINT_TIE:
                    best = (k, score)
            if best is None:
                continue
            rise = open_rises.pop(best[0])
            pairs.append(
                _Pair(
                    (rise.delta_w - edge.delta_w) / 2,
                    rise.ts,
                    edge.ts - rise.ts,
                    rise,
                    edge,
                )
            )
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

    @staticmethod
    def _overlap_share(group: list[_Pair]) -> float:
        """Share of the runs that overlap another run in the group."""
        runs = sorted((p.start_ts, p.start_ts + p.duration_s) for p in group)
        overlapping: set[int] = set()
        latest_end, holder = float("-inf"), -1
        for i, (start, end) in enumerate(runs):
            if start < latest_end:
                overlapping.update((i, holder))
            if end > latest_end:
                latest_end, holder = end, i
        return len(overlapping) / len(runs) if runs else 0.0

    @staticmethod
    def _duration_counts(durations: list[float]) -> tuple[int, ...]:
        counts = [0] * (len(DURATION_EDGES_S) + 1)
        for value in durations:
            counts[bisect_right(DURATION_EDGES_S, value)] += 1
        return tuple(counts)

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
                    recent=tuple(
                        (p.start_ts, p.duration_s, p.level_w)
                        for p in sorted(group, key=lambda p: -p.start_ts)[:RECENT_OCCURRENCES]
                    ),
                    hour_counts=tuple(
                        sum(1 for t in starts if hour_of_day(t) == h) for h in range(24)
                    ),
                    duration_counts=self._duration_counts(durations),
                    overlap_share=self._overlap_share(group),
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
