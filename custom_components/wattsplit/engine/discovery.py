"""Find recurring on/off pairs among edges no device explained.

This is the "inbox" behind the UI: it turns unexplained steps into candidate
appliances ("a ~4.4 kW load, 3 times this week, ~8 min each") that the user can
name, which then become latent devices.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime
from statistics import median

from .models import Edge


@dataclass(frozen=True, slots=True)
class Candidate:
    level_w: float
    count: int
    median_duration_s: float
    typical_hour: int  # local hour, approximate (median; ignores midnight wrap)


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
        max_edges: int = 500,
    ) -> None:
        self._tol = tolerance
        self._max_duration_s = max_duration_s
        self._edges: deque[Edge] = deque(maxlen=max_edges)

    def add(self, edge: Edge) -> None:
        self._edges.append(edge)

    def _pairs(self) -> list[_Pair]:
        edges = sorted(self._edges, key=lambda e: e.ts)
        used: set[int] = set()
        pairs = []
        for i, rise in enumerate(edges):
            if rise.delta_w <= 0 or i in used:
                continue
            for j in range(i + 1, len(edges)):
                fall = edges[j]
                if fall.ts - rise.ts > self._max_duration_s:
                    break
                if j in used or fall.delta_w >= 0:
                    continue
                if abs(-fall.delta_w - rise.delta_w) <= self._tol * rise.delta_w:
                    used.update((i, j))
                    pairs.append(
                        _Pair(
                            (rise.delta_w - fall.delta_w) / 2,
                            rise.ts,
                            fall.ts - rise.ts,
                            rise,
                            fall,
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

    def candidates(self, min_count: int = 2) -> list[Candidate]:
        out = [
            Candidate(
                level_w=median(p.level_w for p in c),
                count=len(c),
                median_duration_s=median(p.duration_s for p in c),
                typical_hour=int(
                    median(datetime.fromtimestamp(p.start_ts).hour for p in c)
                ),
            )
            for c in self._clusters()
            if len(c) >= min_count
        ]
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
