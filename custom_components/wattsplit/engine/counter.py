"""Average power while on, from a cumulative energy counter and the on/off timeline.

Many appliances (air conditioners, washers) report a running energy total, in
Wh or kWh, but no instantaneous power. A counter is a poor tool for real-time
attribution (it may update once an hour, or once every few), yet it is a clean,
interference-free measurement of how much energy the appliance used between two
updates. Combined with how long it was on in that time, it gives its power:

    power while on  =  energy between updates  /  time on in that interval

Summed over many intervals this is a ratio estimator that does not care about
the rest of the house. It is the appliance's own figure, so it is only as good as
the appliance's own metering, and it is used as a starting point and a
cross-check for what the whole-home channels say.
"""

from __future__ import annotations

from collections import deque


class CounterEstimator:
    _KEEP = 120  # intervals remembered
    _MIN_ON_S = 300.0  # an interval needs at least this much on-time to count
    _MIN_W = 10.0  # below this the counter is not measuring a running device
    _MIN_INTERVALS = 2

    def __init__(self) -> None:
        self._events: list[tuple[float, bool]] = []  # (ts, is_on), in time order
        self._last: tuple[float, float] | None = None  # (ts, counter value in Wh)
        # (energy_wh, seconds_on, seconds_total) per counter update
        self._intervals: deque[tuple[float, float, float]] = deque(maxlen=self._KEEP)

    def add_state(self, ts: float, is_on: bool) -> None:
        """Record the on/off timeline (also use it once, with the current state,
        as a baseline)."""
        if self._events and ts < self._events[-1][0]:
            ts = self._events[-1][0]
        self._events.append((ts, is_on))

    def add_counter(self, ts: float, value_wh: float) -> None:
        """Record a counter reading. A reading lower than the last one is a
        rollover (a daily or monthly counter): that interval is dropped."""
        if self._last is not None:
            t0, v0 = self._last
            if ts <= t0:
                return
            if value_wh >= v0:
                self._intervals.append(
                    (value_wh - v0, self._seconds_on(t0, ts), ts - t0)
                )
        self._last = (ts, value_wh)
        self._prune(ts)

    def _state_at(self, ts: float) -> bool:
        state = False
        for event_ts, is_on in self._events:
            if event_ts > ts:
                break
            state = is_on
        return state

    def _seconds_on(self, t0: float, t1: float) -> float:
        on_s, cursor, state = 0.0, t0, self._state_at(t0)
        for event_ts, is_on in self._events:
            if event_ts <= t0:
                continue
            if event_ts >= t1:
                break
            if state:
                on_s += event_ts - cursor
            cursor, state = event_ts, is_on
        if state:
            on_s += t1 - cursor
        return on_s

    def _prune(self, ts: float) -> None:
        """Forget events older than the last one at or before the last reading."""
        keep_from = 0
        for i, (event_ts, _) in enumerate(self._events):
            if event_ts <= ts:
                keep_from = i
        if keep_from:
            self._events = self._events[keep_from:]

    def estimate(self) -> tuple[float, float] | None:
        """``(power_w, confidence)`` or None until there is enough to say."""
        valid = [
            (e, on) for e, on, _ in self._intervals if on >= self._MIN_ON_S and e >= 0
        ]
        if len(valid) < self._MIN_INTERVALS:
            return None
        total_on = sum(on for _, on in valid)
        power = sum(e for e, _ in valid) * 3600.0 / total_on
        if power < self._MIN_W:
            return None
        # How well is the *total* ratio determined? Each interval's energy should
        # be power x on-time; the spread of the residuals (not the spread of the
        # per-interval power, which just reflects how differently the appliance
        # was used) gives the standard error of the ratio estimate.
        n = len(valid)
        hours = [on / 3600.0 for _, on in valid]
        residuals = sum((e - power * h) ** 2 for (e, _), h in zip(valid, hours))
        relative_se = (residuals * n / (n - 1)) ** 0.5 / sum(hours) / power
        confidence = (
            min(1.0, n / 6)
            * min(1.0, total_on / 3600.0)
            * max(0.0, 1.0 - 2.0 * relative_se)
        )
        return power, confidence

    def describe(self) -> dict:
        est = self.estimate()
        return {
            "intervals": len(self._intervals),
            "usable_intervals": sum(1 for _, on, _ in self._intervals if on >= self._MIN_ON_S),
            "power_w": None if est is None else round(est[0], 1),
            "confidence": None if est is None else round(est[1], 2),
        }
