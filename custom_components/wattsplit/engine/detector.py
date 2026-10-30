"""Step detection on regularly sampled power channels."""

from __future__ import annotations

from collections import deque
from statistics import median

from .models import Edge


class StepDetector:
    """Emit an :class:`Edge` when one channel settles at a new level.

    Each sample is compared as ``before`` (median of the last ``ref_s``
    seconds) vs ``after`` (median of the last ``settle_s`` seconds). A step is
    reported only when both windows are flat relative to the step size, so a
    ramp or a noisy transient produces one edge once it settles, not many.

    The smallest step worth reporting is ``min_step_w``. With ``noise_k`` set
    it also adapts to the channel: a step must exceed ``noise_k`` times the
    channel's typical update-to-update jitter, so a quiet circuit shows small
    appliances and a noisy one does not pretend to.

    Samples must arrive on a regular grid of ``dt_s`` seconds. The caller
    (a timer in the HA layer) is responsible for sample-and-hold when the
    source sensor only updates on change.
    """

    _MIN_JITTER_SAMPLES = 10

    def __init__(
        self,
        dt_s: float = 2.0,
        min_step_w: float = 300.0,
        settle_s: float = 6.0,
        ref_s: float = 10.0,
        flatness: float = 0.35,
        noise_k: float | None = None,
        noise_window: int = 300,
        channel: str = "total",
    ) -> None:
        self.channel = channel
        self._settle_n = max(2, round(settle_s / dt_s))
        self._ref_n = max(3, round(ref_s / dt_s))
        self._min_step_w = min_step_w
        self._flatness = flatness
        self._noise_k = noise_k
        self._buf: deque[tuple[float, float]] = deque(
            maxlen=self._settle_n + self._ref_n
        )
        self._last: float | None = None
        self._jitter: deque[float] = deque(maxlen=noise_window)

    @property
    def threshold_w(self) -> float:
        """Smallest step currently reported."""
        if self._noise_k is None or len(self._jitter) < self._MIN_JITTER_SAMPLES:
            return self._min_step_w
        sigma = 1.4826 * median(self._jitter)
        return max(self._min_step_w, self._noise_k * sigma)

    def reset(self) -> None:
        """Forget the buffered samples, e.g. after the source went unavailable."""
        self._buf.clear()
        self._last = None

    def update(self, ts: float, power_w: float) -> Edge | None:
        # Jitter is measured only where the value changed: a sensor that
        # updates every 15 s and is sampled every 2 s repeats its last value.
        if self._last is not None and power_w != self._last:
            self._jitter.append(abs(power_w - self._last))
        self._last = power_w

        self._buf.append((ts, power_w))
        if len(self._buf) < self._buf.maxlen:
            return None

        values = [p for _, p in self._buf]
        before = values[: self._ref_n]
        after = values[self._ref_n :]
        delta = median(after) - median(before)
        if abs(delta) < self.threshold_w:
            return None

        limit = self._flatness * abs(delta)
        if max(before) - min(before) > limit or max(after) - min(after) > limit:
            return None

        return Edge(
            ts=self._buf[self._ref_n][0],
            delta_w=delta,
            seen_ts=ts,
            parts=((self.channel, delta),),
        )


class MultiChannelDetector:
    """One :class:`StepDetector` per channel, merged into whole-house edges.

    A load that spans several channels (a 220 V shower across two phases)
    steps each of them within a few seconds. Significant per-channel steps
    that land within ``merge_s`` of each other become one edge whose
    ``delta_w`` is their sum and whose ``parts`` keep the breakdown. Noisy
    channels only contribute when a step clears *their own* noise level, so
    they do not drown the quiet ones.

    With a single channel nothing is delayed or merged.
    """

    def __init__(
        self,
        channels: list[str],
        merge_s: float | None = None,
        per_channel: dict[str, dict] | None = None,
        **detector_kwargs,
    ) -> None:
        per_channel = per_channel or {}
        self._dets = {
            c: StepDetector(channel=c, **{**detector_kwargs, **per_channel.get(c, {})})
            for c in channels
        }
        self._merge_s = (0.0 if len(channels) == 1 else 6.0) if merge_s is None else merge_s
        self._pending: list[Edge] = []

    @property
    def channels(self) -> list[str]:
        return list(self._dets)

    def thresholds(self) -> dict[str, float]:
        return {c: d.threshold_w for c, d in self._dets.items()}

    def reset(self, channel: str | None = None) -> None:
        for name, det in self._dets.items():
            if channel is None or name == channel:
                det.reset()

    def update(self, ts: float, readings: dict[str, float]) -> Edge | None:
        for channel, value in readings.items():
            det = self._dets.get(channel)
            if det is not None and (edge := det.update(ts, value)) is not None:
                self._pending.append(edge)
        if not self._pending or ts - (self._pending[0].seen_ts or ts) < self._merge_s:
            return None

        parts = tuple(p for e in self._pending for p in e.parts)
        edge = Edge(
            ts=min(e.ts for e in self._pending),
            delta_w=sum(delta for _, delta in parts),
            seen_ts=ts,
            parts=parts,
        )
        self._pending = []
        return edge


class GlitchFilter:
    """Treats an impossible reading as missing instead of as a real step.

    Some derived sensors report exactly 0 W for a while when one of their
    inputs drops out. For a consumption signal that was recently well above
    zero, an exact 0 is held back for up to ``max_hold_s``; if it persists it is
    accepted as real. Do not use it on net-grid channels, where 0 is legitimate.
    """

    def __init__(self, max_hold_s: float = 90.0, min_prev_w: float = 100.0) -> None:
        self._max_hold_s = max_hold_s
        self._min_prev_w = min_prev_w
        self._last_good: float | None = None
        self._bad_since: float | None = None

    def filter(self, ts: float, value: float | None) -> float | None:
        if value is None:
            return None
        suspicious = (
            value == 0.0
            and self._last_good is not None
            and self._last_good >= self._min_prev_w
        )
        if suspicious:
            if self._bad_since is None:
                self._bad_since = ts
            if ts - self._bad_since < self._max_hold_s:
                return None
        self._bad_since = None
        self._last_good = value
        return value
