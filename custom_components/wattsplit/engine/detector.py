"""Streaming step detector for a regularly sampled power signal."""

from __future__ import annotations

from collections import deque
from statistics import median

from .models import Edge


class StepDetector:
    """Emit an :class:`Edge` when the signal settles at a new level.

    Each sample is compared as ``before`` (median of the last ``ref_s``
    seconds) vs ``after`` (median of the last ``settle_s`` seconds). A step is
    reported only when both windows are flat relative to the step size, so a
    ramp or a noisy transient produces one edge once it settles, not many.

    Samples must arrive on a regular grid of ``dt_s`` seconds. The caller
    (a timer in the HA layer) is responsible for sample-and-hold when the
    source sensor only updates on change.
    """

    def __init__(
        self,
        dt_s: float = 2.0,
        min_step_w: float = 300.0,
        settle_s: float = 6.0,
        ref_s: float = 10.0,
        flatness: float = 0.35,
    ) -> None:
        self._settle_n = max(2, round(settle_s / dt_s))
        self._ref_n = max(3, round(ref_s / dt_s))
        self._min_step_w = min_step_w
        self._flatness = flatness
        self._buf: deque[tuple[float, float]] = deque(
            maxlen=self._settle_n + self._ref_n
        )

    def update(self, ts: float, power_w: float) -> Edge | None:
        self._buf.append((ts, power_w))
        if len(self._buf) < self._buf.maxlen:
            return None

        values = [p for _, p in self._buf]
        before = values[: self._ref_n]
        after = values[self._ref_n :]
        delta = median(after) - median(before)
        if abs(delta) < self._min_step_w:
            return None

        limit = self._flatness * abs(delta)
        if max(before) - min(before) > limit or max(after) - min(after) > limit:
            return None

        return Edge(ts=self._buf[self._ref_n][0], delta_w=delta, seen_ts=ts)
