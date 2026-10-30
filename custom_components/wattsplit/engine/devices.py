"""Per-device models: latent (inferred from edges) and state-driven (learned)."""

from __future__ import annotations

from collections import deque
from dataclasses import replace
from statistics import median

from .models import DeviceSpec, PowerMode


class LatentDevice:
    """A device with no entity at all, e.g. an electric shower."""

    def __init__(self, spec: DeviceSpec) -> None:
        self.spec = spec
        self.level_w = 0.0
        self.since: float | None = None

    @property
    def awaiting_discovery(self) -> bool:
        return not self.spec.power_levels_w

    @property
    def is_on(self) -> bool:
        return self.level_w > 0

    def set_levels(self, levels_w: tuple[float, ...]) -> None:
        self.spec = replace(self.spec, power_levels_w=levels_w)

    def match(self, delta_w: float) -> tuple[float, float] | None:
        """Return ``(new_level_w, relative_error)`` if the edge fits, else None."""
        best: tuple[float, float] | None = None
        for new_level in (0.0, *self.spec.power_levels_w):
            if new_level == self.level_w:
                continue
            expected = new_level - self.level_w
            if expected * delta_w <= 0:
                continue
            err = abs(delta_w - expected) / abs(expected)
            if err <= self.spec.tolerance and (best is None or err < best[1]):
                best = (new_level, err)
        return best

    def apply(self, new_level_w: float, ts: float) -> None:
        if new_level_w > 0 and self.level_w == 0:
            self.since = ts
        elif new_level_w == 0:
            self.since = None
        self.level_w = new_level_w


class StateDevice:
    """An entity that reports on/off but has no power sensor, e.g. an A/C."""

    _HISTORY = 40
    _PRIOR_BAND = 0.5  # reject steps further than this fraction from the prior

    def __init__(self, spec: DeviceSpec) -> None:
        self.spec = spec
        self.is_on = False
        self._steps: deque[float] = deque(maxlen=self._HISTORY)

    @property
    def frozen(self) -> bool:
        """True when the power is declared/pinned and must not be learned."""
        return self.spec.mode in (PowerMode.KNOWN, PowerMode.PINNED)

    @property
    def prior_w(self) -> float | None:
        return self.spec.power_levels_w[0] if self.spec.power_levels_w else None

    @property
    def learned_w(self) -> float | None:
        if self.frozen:
            return self.prior_w
        if self._steps:
            return median(self._steps)
        return self.prior_w

    @property
    def confidence(self) -> float:
        if self.frozen:
            return 1.0 if self.spec.mode is PowerMode.PINNED else 0.9
        n = len(self._steps)
        if n == 0:
            return 0.2 if self.prior_w else 0.0
        spread = median(abs(s - median(self._steps)) for s in self._steps)
        rel = spread / median(self._steps)
        return min(1.0, n / 10) * max(0.0, 1 - 2 * rel)

    @property
    def power_w(self) -> float:
        return (self.learned_w or 0.0) if self.is_on else 0.0

    def plausible(self, magnitude_w: float) -> bool:
        ref = self.learned_w
        if self.frozen or ref is None:
            return True
        return abs(magnitude_w - ref) <= self._PRIOR_BAND * ref

    def observe_step(self, magnitude_w: float) -> None:
        if not self.frozen:
            self._steps.append(magnitude_w)

    def pin(self) -> None:
        """Freeze the currently learned power."""
        learned = self.learned_w
        if learned is not None:
            self.spec = replace(
                self.spec, power_levels_w=(learned,), power_mode=PowerMode.PINNED
            )
