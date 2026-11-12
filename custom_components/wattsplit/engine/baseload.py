"""The always-on consumption of a home: what it draws when nothing is going on."""

from __future__ import annotations

from collections import deque


class BaseLoad:
    """Low end of the whole-home power over the last day.

    Power is averaged per minute (so a one-second dip or a glitch does not set the
    floor) and the base load is a low percentile of those minutes: the fridge, the
    router, the standby of everything. It is what a home spends even when nobody is
    using anything, and the part of "untracked" that no appliance will ever explain.
    Needs an hour of data before it says anything.
    """

    def __init__(
        self,
        window_s: float = 86_400.0,
        bucket_s: float = 60.0,
        percentile: float = 0.05,
        min_buckets: int = 60,
    ) -> None:
        self._window_s = window_s
        self._bucket_s = bucket_s
        self._q = percentile
        self._min = min_buckets
        self._means: deque[tuple[float, float]] = deque()  # (bucket start, mean W)
        self._bucket: float | None = None
        self._sum = 0.0
        self._n = 0

    def add(self, ts: float, watts: float | None) -> None:
        if watts is None:
            return
        bucket = ts - (ts % self._bucket_s)
        if self._bucket is not None and bucket != self._bucket and self._n:
            self._means.append((self._bucket, self._sum / self._n))
            self._sum, self._n = 0.0, 0
        self._bucket = bucket
        self._sum += watts
        self._n += 1
        horizon = ts - self._window_s
        while self._means and self._means[0][0] < horizon:
            self._means.popleft()

    def value(self) -> float | None:
        if len(self._means) < self._min:
            return None
        ordered = sorted(w for _, w in self._means)
        return ordered[int(self._q * (len(ordered) - 1))]
