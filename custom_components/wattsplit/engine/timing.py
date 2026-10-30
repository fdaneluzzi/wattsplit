"""Time constants derived from how often each sensor actually reports.

Sensors differ a lot: a local meter may report every second, a grid meter every
15 s, a cloud solar inverter every 60 s or slower. Nothing in the engine assumes
a rate. The Home Assistant layer measures each input's reporting period with a
:class:`RateTracker` and asks :func:`timing_for` how long the detection and
averaging windows must be for the slowest input that matters.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from statistics import median


class RateTracker:
    """Estimates how often one input reports a new value."""

    def __init__(self, keep: int = 20) -> None:
        self._last: float | None = None
        self._gaps: deque[float] = deque(maxlen=keep)

    def observe(self, ts: float) -> None:
        """Call once per *new* report from the input."""
        if self._last is not None and ts > self._last:
            self._gaps.append(ts - self._last)
        self._last = ts

    @property
    def period_s(self) -> float | None:
        """Median gap between reports, or None until a few have been seen."""
        return median(self._gaps) if len(self._gaps) >= 3 else None


@dataclass(frozen=True, slots=True)
class Timing:
    settle_s: float  # a new level must hold this long to count as a step
    ref_s: float  # the level before it is judged over this long
    merge_s: float  # per-channel steps this close are one event
    skip_s: float  # drop this long after a state change (start-up transient)
    window_s: float  # averaging window before / after a state change
    min_window_s: float  # shorter windows are not trusted

    @property
    def shortest_run_s(self) -> float:
        """Shortest on (or off) period this channel can measure.

        A run must leave a usable window after the start-up transient and
        before the guard that precedes the next change.
        """
        return self.skip_s + 5.0 + self.min_window_s


def timing_for(period_s: float | None, dt_s: float) -> Timing:
    """Windows for inputs that report every ``period_s`` seconds.

    ``dt_s`` is the engine's own sampling tick. With ``period_s`` unknown the
    defaults assume a fast sensor. The rules keep at least a couple of fresh
    readings inside every window, so a slow sensor gets longer windows and a
    fast one stays responsive. A window needs at least two reports to say
    anything, which is the floor: a 61 s sensor resolves runs of about 3.6
    minutes and longer (see ``Timing.shortest_run_s``). Anything faster than
    that the estimators simply do not reach confidence on; they never guess.
    """
    t = period_s or 0.0
    return Timing(
        settle_s=max(3 * dt_s, 1.5 * t),
        ref_s=max(5 * dt_s, 2.5 * t),
        merge_s=max(3 * dt_s, 1.2 * t),
        skip_s=max(40.0, 1.5 * t),
        window_s=max(120.0, 5 * t),
        min_window_s=max(15.0, 2 * t),
    )
