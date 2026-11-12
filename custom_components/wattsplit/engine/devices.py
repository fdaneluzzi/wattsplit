"""Per-device models: latent (inferred from edges) and state-driven (learned)."""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field, replace
from statistics import median

from .counter import CounterEstimator
from .models import DeviceSpec, Edge, PowerMode, Toggle


class LatentDevice:
    """A device with no entity at all, e.g. an electric shower."""

    _MIN_RUNS = 5  # closed runs before its own history can bound a run
    # Until it has that history a run is capped at this long: nothing that runs on a
    # kitchen counter or in a utility room is meant to stay on for hours unseen.
    DEFAULT_RUN_LIMIT_S = 7200.0
    # Runs past the limit that still ended with a down-step are real if several of
    # them last about the same (a long cycle), and stuck if they are all over the place.
    _OVERLONG_NEEDED = 3
    _OVERLONG_SPREAD = 2.0
    _MIN_RESTART_AGE_S = 30.0  # a "restart" this soon after starting is something else

    def __init__(self, spec: DeviceSpec) -> None:
        self.spec = spec
        self.level_w = 0.0
        self.since: float | None = None
        self.runs: deque[float] = deque(maxlen=50)  # durations of runs that ended normally
        self.forced_closes = 0  # runs ended without seeing their down-step
        self.overlong: deque[float] = deque(maxlen=5)  # ended by a down-step past the limit

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

    def restart_match(self, delta_w: float, ts: float) -> tuple[float, float] | None:
        """``(level_w, relative_error)`` if this rise is the device starting again.

        A device that is already on cannot start: its down-step was missed (it
        merged with another load, or was claimed by someone else). A rise the
        size of one of its own levels is then most likely its next run, and the
        old run must end here instead of waiting for a fall that never comes.
        """
        if not self.is_on or delta_w <= 0 or self.since is None:
            return None
        if ts - self.since < self._MIN_RESTART_AGE_S:
            return None
        best: tuple[float, float] | None = None
        for level in self.spec.power_levels_w:
            err = abs(delta_w - level) / level
            if err <= self.spec.tolerance and (best is None or err < best[1]):
                best = (level, err)
        return best

    def restart(self, level_w: float, ts: float) -> None:
        """End the current run without a down-step and start a new one."""
        self.forced_closes += 1
        self.since = ts
        self.level_w = level_w

    def force_close(self) -> None:
        """End the run because the house power says it cannot still be going."""
        self.forced_closes += 1
        self.since = None
        self.level_w = 0.0

    def run_limit_s(self) -> float:
        """How long a run may last before it is surely a missed down-step.

        With no history (fewer than five finished runs) it is ``DEFAULT_RUN_LIMIT_S``.
        After that it follows the device's own runs, using the median and the third
        quartile rather than the largest values, so that a stuck run among the first
        few cannot raise it for good: at least 15 minutes, eight times the median,
        twice the third quartile. A device whose real cycle is longer than that
        teaches it by ending three similar runs past the limit.
        """
        if len(self.runs) < self._MIN_RUNS:
            limit = self.DEFAULT_RUN_LIMIT_S
        else:
            ordered = sorted(self.runs)
            mid = median(ordered)
            q3 = ordered[min(len(ordered) - 1, int(0.75 * len(ordered)))]
            limit = max(900.0, 8.0 * mid, 2.0 * q3)
        recent = list(self.overlong)[-self._OVERLONG_NEEDED :]
        if len(recent) == self._OVERLONG_NEEDED and max(recent) <= self._OVERLONG_SPREAD * min(recent):
            limit = max(limit, 1.3 * max(recent))
        return limit

    def apply(self, new_level_w: float, ts: float) -> None:
        if new_level_w > 0 and self.level_w == 0:
            self.since = ts
        elif new_level_w == 0:
            if self.since is not None:
                duration = ts - self.since
                if duration <= self.run_limit_s():
                    self.runs.append(duration)
                else:
                    self.overlong.append(duration)
            self.since = None
        self.level_w = new_level_w


@dataclass(slots=True)
class _Event:
    ts: float
    is_on: bool
    # (magnitude_w, lag_s) of every step that could have been this event's
    candidates: list[tuple[float, float]] = field(default_factory=list)


class StateDevice:
    """An entity that reports on/off but has no power sensor, e.g. an A/C.

    Two independent kinds of evidence are collected for every state change, and
    the device's power comes from whichever is trustworthy:

    * **Channel means** (:class:`~.models.Toggle`): for each measured channel,
      the change in mean power between a window before and a window after the
      change. A channel is *involved* when the median change is clearly above
      its own scatter. A quiet channel isolates a small appliance even when
      others are full of unrelated loads, and it measures the real average of
      an appliance whose power ramps.
    * **Step consensus**: the steps that follow each state change, within the
      match window. Unrelated loads rarely repeat the same size after every
      toggle; the device's own step does. This works on a single busy channel
      whose power moves in steps rather than drifting.

    When neither is conclusive the device reports that it does not know yet
    instead of guessing.
    """

    _EVENTS = 20
    # One record per channel per state change, so this is channels x events. It
    # was 40 (only 10 events per channel with four channels), which capped the
    # evidence every channel could accumulate.
    _TOGGLES = 600
    _ADMIT_BAND = 0.5  # with a prior, ignore evidence further than this from it
    _CYCLE_BAND = 0.12  # a step this close to a cycling level is that cycle
    _CLAIM_BAND = 0.25  # an edge this close to the known power is ours
    _MATCH_TOL = 0.15  # steps this close to each other are "the same" power
    _MIN_N = 3  # toggles per channel before a channel can be trusted
    # median / standard error needed to call a channel involved. High on
    # purpose: with ~10 toggles and heavy-tailed noise, z = 3 lets a noisy
    # channel through by chance a few percent of the time; a real contribution
    # on a quiet channel clears 6 by a wide margin.
    _MIN_Z = 6.0
    _MIN_W = 20.0  # contributions below this are not distinguishable from drift
    _MIN_CONSISTENCY = 0.8  # share of toggles that must show the contribution
    _PART_BAND = 0.35  # a channel's share of a merged edge matches within this
    _PART_COVERAGE = 0.6  # ...and the matched channels must cover this much of it
    _PROVISIONAL_Z = 2.0  # looser bar, for a display-only guess

    def __init__(self, spec: DeviceSpec, window_s: float = 90.0) -> None:
        self.spec = spec
        self.is_on = False
        self._window = window_s
        self._events: deque[_Event] = deque(maxlen=self._EVENTS)
        self._toggles: deque[Toggle] = deque(maxlen=self._TOGGLES)
        self.counter = CounterEstimator()
        self.cycle_levels: set[float] = set(spec.cycle_levels_w)  # confirmed + learned

    @property
    def frozen(self) -> bool:
        """True when the power is declared/pinned and must not be learned."""
        return self.spec.mode in (PowerMode.KNOWN, PowerMode.PINNED)

    @property
    def prior_w(self) -> float | None:
        return self.spec.power_levels_w[0] if self.spec.power_levels_w else None

    # -- inputs -----------------------------------------------------------------

    def open_event(self, ts: float, is_on: bool) -> None:
        self.is_on = is_on
        self.counter.add_state(ts, is_on)
        self._events.append(_Event(ts, is_on))

    def add_counter(self, ts: float, value_wh: float) -> None:
        """A reading of this device's own cumulative energy counter."""
        self.counter.add_counter(ts, value_wh)

    def add_toggle(self, toggle: Toggle) -> None:
        if self.frozen:
            return
        prior = self.prior_w
        if prior is not None:
            # A toggle is one channel's view of the change, which can be only
            # part of the device's power (or none). Only reject views that are
            # far *larger* than the prior: another load switched at the same time.
            diffs = self._signed_diffs(toggle).values()
            if any(abs(d) > (1 + self._ADMIT_BAND) * prior for d in diffs):
                return
        self._toggles.append(toggle)

    @staticmethod
    def _signed_diffs(toggle: Toggle) -> dict[str, float]:
        before, after = dict(toggle.before), dict(toggle.after)
        sign = 1.0 if toggle.is_on else -1.0
        return {c: sign * (after[c] - before[c]) for c in after if c in before}

    def _recent_event(self, ts: float, turned_on: bool) -> _Event | None:
        """The latest state change of this kind shortly before ``ts``."""
        for ev in reversed(self._events):
            lag = ts - ev.ts
            if lag > self._window:
                return None
            if lag >= -2.0 and ev.is_on == turned_on:
                return ev
        return None

    @property
    def footprint(self) -> dict[str, float]:
        """Where this device shows up, per channel, once that is statistically clear."""
        if self.frozen:
            return {}
        return {c: m for c, m, *_ in self._involved()}

    def claim_parts(self, edge: Edge) -> dict[str, float]:
        """The parts of a merged edge that match this device's footprint.

        Two things switching in the same meter report arrive as one edge. A
        device that knows which channels it lives on can take its share of it
        (its own channels, with the right sign, a state change just before) and
        leave the rest for others. Needs a clear footprint and most of it present.
        """
        footprint = self.footprint
        if not footprint or len(edge.parts) < 2:
            return {}
        claimed = {}
        for channel, part in edge.parts:
            reference = footprint.get(channel)
            if reference is None or self._recent_event(edge.ts, part > 0) is None:
                continue
            if abs(abs(part) - reference) <= self._PART_BAND * reference:
                claimed[channel] = part
        covered = sum(abs(p) for p in claimed.values())
        return claimed if covered >= self._PART_COVERAGE * sum(footprint.values()) else {}

    def offer(self, edge: Edge) -> float | None:
        """Consider an edge. Returns its relative error if this device *claims*
        it, else None. Plausible steps are remembered as consensus candidates
        either way."""
        magnitude = abs(edge.delta_w)
        if self.is_on and self.cycle_levels:
            # While it runs, a compressor or heater cycling on and off is this
            # device's own doing, at any moment, not only right after a state change.
            near = min(self.cycle_levels, key=lambda level: abs(magnitude - level))
            if abs(magnitude - near) <= self._CYCLE_BAND * near:
                return abs(magnitude - near) / near
        if self.frozen and self.prior_w is None:
            return None
        event = self._recent_event(edge.ts, edge.delta_w > 0)
        if event is None:
            return None

        reference = self._reference()
        claimed = (
            reference is not None
            and abs(magnitude - reference) <= self._CLAIM_BAND * reference
        )
        prior = self.prior_w
        admitted = prior is None or abs(magnitude - prior) <= self._ADMIT_BAND * prior
        if not self.frozen and (claimed or admitted):
            event.candidates.append((magnitude, edge.ts - event.ts))
        return abs(magnitude - reference) / reference if claimed else None

    # -- evidence 1: channel means ----------------------------------------------------

    def _channel_stats(self) -> list[tuple[str, float, float, float, int, float]]:
        """``(channel, median_w, std_error_w, scatter_w, n, consistency)``.

        ``consistency`` is the share of toggles whose change on that channel is
        at least half the median: a real contribution shows up on nearly every
        toggle, while a lucky median of unrelated loads does not.
        """
        per_channel: dict[str, list[float]] = {}
        for toggle in self._toggles:
            for channel, diff in self._signed_diffs(toggle).items():
                per_channel.setdefault(channel, []).append(diff)
        stats = []
        for channel, diffs in per_channel.items():
            n = len(diffs)
            med = median(diffs)
            scatter = 1.4826 * median(abs(d - med) for d in diffs)
            consistent = sum(d >= 0.5 * med for d in diffs) / n
            stats.append(
                (channel, med, 1.25 * max(scatter, 1.0) / math.sqrt(n), scatter, n, consistent)
            )
        return stats

    def _involved(self) -> list[tuple[str, float, float, float, int, float]]:
        return [
            s
            for s in self._channel_stats()
            if s[4] >= self._MIN_N
            and s[1] >= self._MIN_W
            and s[1] / s[2] >= self._MIN_Z
            and s[5] >= self._MIN_CONSISTENCY
        ]

    def _channel_estimate(self) -> tuple[float, float] | None:
        """``(power_w, confidence)`` from channel means, or None."""
        involved = self._involved()
        if not involved:
            return None
        power = sum(m for _, m, *_ in involved)
        se = math.sqrt(sum(s[2] ** 2 for s in involved))
        spread = math.sqrt(sum(s[3] ** 2 for s in involved)) / power
        n = min(s[4] for s in involved)
        conf = min(1.0, n / 6) * min(1.0, power / se / 6) * max(0.0, 1 - spread)
        return power, conf

    @property
    def channel_powers(self) -> tuple[tuple[str, float], ...]:
        return tuple((c, round(m, 1)) for c, m, *_ in self._involved())

    # -- evidence 2: step consensus ---------------------------------------------------

    def _consensus(self) -> tuple[float, int, float, int] | None:
        """``(power_w, supporting_events, relative_spread, rival_support)``.

        ``rival_support`` is the support of the best magnitude that is clearly
        different from the winner. When it is close to the winner's, the data
        cannot tell the two apart and callers must not be confident.
        """
        events = [e for e in self._events if e.candidates]
        if not events:
            return None
        prior = self.prior_w
        best_key = None
        best: tuple[float, int, float] | None = None
        scored: list[tuple[float, int]] = []
        for center in sorted({m for e in events for m, _ in e.candidates}):
            matched = []
            for e in events:
                near = [
                    c
                    for c in e.candidates
                    if abs(c[0] - center) <= self._MATCH_TOL * center
                ]
                if near:
                    matched.append(min(near, key=lambda c: abs(c[0] - center)))
            mags = [m for m, _ in matched]
            med = median(mags)
            spread = median(abs(m - med) for m in mags) / med
            lag = sum(lag for _, lag in matched) / len(matched)
            closeness = -abs(med - prior) / prior if prior else 0.0
            scored.append((med, len(matched)))
            key = (len(matched), -spread, closeness, -lag)
            if best_key is None or key > best_key:
                best_key, best = key, (med, len(matched), spread)
        assert best is not None
        rival = max(
            (
                support
                for med, support in scored
                if abs(med - best[0]) > 2 * self._MATCH_TOL * best[0]
            ),
            default=0,
        )
        return (*best, rival)

    def _consensus_estimate(self) -> tuple[float, float] | None:
        """``(power_w, confidence)`` from step consensus, or None if ambiguous."""
        cons = self._consensus()
        if cons is None:
            return None
        power, support, spread, rival = cons
        if support < self._MIN_N or rival >= support:
            return None
        closed = sum(1 for e in self._events if e.candidates) or support
        conf = (
            min(1.0, support / 6)
            * (support / max(closed, support))
            * max(0.0, 1 - 2 * spread)
            * (1 - rival / support)
        )
        return power, conf

    # -- what we believe --------------------------------------------------------------

    _COUNTER_MIN_CONFIDENCE = 0.3

    def _counter_estimate(self) -> tuple[float, float] | None:
        est = self.counter.estimate()
        return est if est and est[1] >= self._COUNTER_MIN_CONFIDENCE else None

    def _best(self) -> tuple[float, float] | None:
        """The evidence to believe.

        The appliance's own energy counter if it is conclusive: it is clean and
        immune to whatever else is switching in the house. Otherwise channel
        means when a channel is statistically clear (they cover every channel the
        device shows up on, which step consensus cannot), else step consensus.
        """
        return (
            self._counter_estimate()
            or self._channel_estimate()
            or self._consensus_estimate()
        )

    @property
    def source(self) -> str:
        if self.frozen:
            return "declared"
        if self._counter_estimate():
            return "learned_from_counter"
        return "learned_from_state"

    def cross_check(self) -> dict | None:
        """Compare what the whole-home channels say with the energy counter.

        Both are independent measurements of the same appliance; agreement is
        evidence for each, divergence says one of them is wrong here. When no
        channel is statistically clear yet, the looser provisional estimate is
        compared instead and marked ``weak``.
        """
        counter = self.counter.estimate()
        if counter is None:
            return None
        channels = self._channel_estimate()
        strength = "strict"
        if channels is None:
            weak = self.provisional_w
            if not weak:
                return None
            channels, strength = (weak, 0.0), "weak"
        return {
            "counter_w": round(counter[0], 1),
            "channels_w": round(channels[0], 1),
            "difference_pct": round(100.0 * (channels[0] - counter[0]) / counter[0], 1),
            "strength": strength,
        }

    def _reference(self) -> float | None:
        """The power used to decide whether an edge is ours."""
        if self.frozen:
            return self.prior_w
        best = self._best()
        return best[0] if best else self.prior_w

    @property
    def learned_w(self) -> float | None:
        return self._reference()

    @property
    def provisional_w(self) -> float | None:
        """Best guess from too little evidence to be believed; for display only.

        Sums the channels that already lean the same way on most toggles (at
        least ``_PROVISIONAL_Z`` standard errors from zero and consistent), so a
        channel that is pure noise does not inflate it. None if no channel does.
        """
        parts = [
            m
            for _, m, se, _, n, k in self._channel_stats()
            if n >= 2 and m >= self._MIN_W and m / se >= self._PROVISIONAL_Z
            and k >= self._MIN_CONSISTENCY
        ]
        return sum(parts) if parts else None

    @property
    def confidence(self) -> float:
        if self.frozen:
            return 1.0 if self.spec.mode is PowerMode.PINNED else 0.9
        best = self._best()
        if best is None:
            return 0.2 if self.prior_w else 0.0
        return best[1]

    @property
    def power_w(self) -> float:
        return (self.learned_w or 0.0) if self.is_on else 0.0

    def describe(self) -> dict:
        cons = self._consensus()
        return {
            "mode": self.spec.mode.value,
            "is_on": self.is_on,
            "learned_w": self.learned_w,
            "confidence": round(self.confidence, 3),
            "toggles": len(self._toggles),
            "channels": [
                {"channel": c, "median_w": round(m, 1), "std_error_w": round(se, 1),
                 "scatter_w": round(sc, 1), "n": n, "consistency": round(k, 2),
                 "involved": any(i[0] == c for i in self._involved())}
                for c, m, se, sc, n, k in self._channel_stats()
            ],
            "counter": self.counter.describe(),
            "cycle_levels_w": sorted(round(x) for x in self.cycle_levels),
            "step_consensus": None if cons is None else {
                "power_w": round(cons[0], 1), "support": cons[1], "rival": cons[3]},
        }

    def pin(self) -> None:
        """Freeze the currently learned power."""
        learned = self.learned_w
        if learned is not None:
            self.spec = replace(
                self.spec, power_levels_w=(learned,), power_mode=PowerMode.PINNED
            )
