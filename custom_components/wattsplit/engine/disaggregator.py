"""Attribute measured power changes to devices."""

from __future__ import annotations

from bisect import bisect_right
from collections import deque
from dataclasses import dataclass, field, replace
from statistics import fmean

from .clock import hour_of_day
from .devices import LatentDevice, StateDevice
from .discovery import Candidate, Discoverer
from .models import DeviceSpec, Edge, Estimate, Toggle


@dataclass(slots=True)
class _PendingToggle:
    device_id: str
    ts: float
    is_on: bool
    prev_ts: float | None
    next_ts: float | None = None
    done: set[str] = field(default_factory=set)  # channels already evaluated


class Disaggregator:
    """Routes each :class:`Edge` to the device that best explains it, and
    learns state devices' power from the channels around their state changes.

    Edge priority:

    1. A **state device** that already knows its power (declared, pinned, a
       prior, or learned) and sees a step of that size shortly after one of its
       state changes *claims* the edge; the closest match wins.
    2. The **latent device** with the smallest relative error against its
       known power levels.
    3. Otherwise the edge is unexplained: it goes to the :class:`Discoverer`
       and stays in the "untracked" bucket downstream.

    Learning for state devices does not depend on edges at all: ``on_sample``
    keeps recent per-channel samples and, once the windows around a state
    change are complete, produces a :class:`Toggle` record.

    ``match_window_s`` is how long after a state change its step may appear.
    ``before_s`` / ``after_s`` size the averaging windows, and ``skip_s`` drops
    the start-up transient after a change.
    """

    _GUARD_S = 5.0
    _MIN_SAMPLES = 4
    # An inferred run cannot draw more than the whole home does. If the home draws
    # less than this share of what the running inferred devices claim, for this long,
    # a down-step was missed and the largest run is closed.
    _INCOHERENT_FRACTION = 0.5
    _INCOHERENT_S = 45.0
    # Closing an open run is evidence a state device's claim must beat by this factor.
    _CLOSE_BONUS = 0.5
    _MIN_REMAINDER_W = 60.0  # what is left of a split edge below this is noise

    def __init__(
        self,
        specs: list[DeviceSpec],
        match_window_s: float = 90.0,
        discoverer: Discoverer | None = None,
        before_s: float = 120.0,
        after_s: float = 120.0,
        skip_s: float = 40.0,
        min_window_s: float = 15.0,
        track_toggles: bool = True,
    ) -> None:
        self.discovery = discoverer or Discoverer()
        self._default_timing = (before_s, after_s, skip_s, min_window_s)
        self._channel_timing: dict[str, tuple[float, float, float, float]] = {}
        self._track = track_toggles
        self._latent: dict[str, LatentDevice] = {}
        self._state: dict[str, StateDevice] = {}
        self._measured: set[str] = set()
        for spec in specs:
            if spec.has_power:
                self._measured.add(spec.device_id)
            elif spec.has_state:
                self._state[spec.device_id] = StateDevice(spec, match_window_s)
            else:
                self._latent[spec.device_id] = LatentDevice(spec)
        self._samples: dict[str, deque[tuple[float, float]]] = {}
        self._pending: list[_PendingToggle] = []
        self._last_event: dict[str, float] = {}
        # When each state device was on, to tell which one a recurring load belongs to.
        self._on_since: dict[str, float] = {}
        self._on_intervals: dict[str, list[tuple[float, float]]] = {d: [] for d in self._state}
        self._t_first: float | None = None
        self._t_last = 0.0
        self._incoherent_since: float | None = None

    # -- state changes and samples --------------------------------------------

    def on_state(self, device_id: str, is_on: bool, ts: float) -> None:
        dev = self._state.get(device_id)
        if dev is None:
            return
        dev.open_event(ts, is_on)
        self._track_on(device_id, is_on, ts)
        if not self._track:
            return
        for pending in self._pending:
            if pending.device_id == device_id and pending.next_ts is None:
                pending.next_ts = ts
        self._pending.append(
            _PendingToggle(device_id, ts, is_on, self._last_event.get(device_id))
        )
        self._last_event[device_id] = ts

    def _track_on(self, device_id: str, is_on: bool, ts: float) -> None:
        self._t_first = ts if self._t_first is None else min(self._t_first, ts)
        self._t_last = max(self._t_last, ts)
        if is_on:
            self._on_since.setdefault(device_id, ts)
        elif device_id in self._on_since:
            self._on_intervals[device_id].append((self._on_since.pop(device_id), ts))

    def sync_state(
        self, device_id: str, is_on: bool, ts: float | None = None
    ) -> None:
        """Set a state device's on/off without treating it as a transition.

        For startup and reconnects, where the entity did not actually switch.
        With ``ts`` it also becomes the baseline of the on/off timeline that
        energy counters are measured against.
        """
        dev = self._state.get(device_id)
        if dev is not None:
            dev.is_on = is_on
            if ts is not None:
                dev.counter.add_state(ts, is_on)
                self._track_on(device_id, is_on, ts)

    def _timing(self, channel: str) -> tuple[float, float, float, float]:
        return self._channel_timing.get(channel, self._default_timing)

    def on_sample(self, ts: float, readings: dict[str, float]) -> list[Toggle]:
        """Record one tick of per-channel power; return finished toggles.

        Each channel is evaluated with its own windows, so a slow channel (a
        cloud inverter reporting every minute) does not hold back a fast one,
        and a toggle can finish on some channels before others. The caller
        should persist the returned records (see ``EdgeLog``).
        """
        for channel, value in readings.items():
            before_s, after_s, skip_s, _ = self._timing(channel)
            samples = self._samples.setdefault(channel, deque())
            samples.append((ts, value))
            horizon = ts - (before_s + skip_s + after_s + 60.0)
            while samples and samples[0][0] < horizon:
                samples.popleft()

        done = []
        for pending in list(self._pending):
            for channel in self._samples:
                if channel in pending.done:
                    continue
                _, after_s, skip_s, _ = self._timing(channel)
                ready = ts >= pending.ts + skip_s + after_s or (
                    pending.next_ts is not None and ts >= pending.next_ts
                )
                if not ready:
                    continue
                pending.done.add(channel)
                toggle = self._finalize(pending, channel, ts)
                if toggle is not None:
                    self._state[toggle.device_id].add_toggle(toggle)
                    done.append(toggle)
            slowest = max(
                (self._timing(c)[2] + self._timing(c)[1] for c in self._samples),
                default=0.0,
            )
            if self._samples.keys() <= pending.done or ts - pending.ts > slowest + 120.0:
                self._pending.remove(pending)
        return done

    def go_live(self) -> None:
        """Start tracking state changes for learning (a replayed engine is not)."""
        self._track = True

    def set_timing(
        self,
        before_s: float,
        after_s: float,
        skip_s: float,
        min_window_s: float,
        channel: str | None = None,
    ) -> None:
        """Change the averaging windows, e.g. once the input rates are known.

        For one ``channel``, or as the default for channels without their own.
        """
        values = (before_s, after_s, skip_s, min_window_s)
        if channel is None:
            self._default_timing = values
        else:
            self._channel_timing[channel] = values

    def on_counter(self, device_id: str, ts: float, value_wh: float) -> None:
        """A reading of an appliance's own cumulative energy counter, in Wh."""
        dev = self._state.get(device_id)
        if dev is not None:
            dev.add_counter(ts, value_wh)

    def cross_checks(self) -> dict[str, dict]:
        """Per appliance, whole-home channels versus its own energy counter."""
        return {k: c for k, v in self._state.items() if (c := v.cross_check())}

    def apply_toggle(self, toggle: Toggle) -> None:
        """Feed a previously recorded toggle (replay)."""
        dev = self._state.get(toggle.device_id)
        if dev is not None:
            dev.add_toggle(toggle)

    def _finalize(self, p: _PendingToggle, channel: str, now: float) -> Toggle | None:
        before_s, after_s, skip_s, min_window_s = self._timing(channel)
        before_end = p.ts - self._GUARD_S
        before_start = p.ts - before_s
        if p.prev_ts is not None:
            before_start = max(before_start, p.prev_ts + skip_s)
        after_start = p.ts + skip_s
        after_end = after_start + after_s
        if p.next_ts is not None:
            after_end = min(after_end, p.next_ts - self._GUARD_S)
        if (
            before_end - before_start < min_window_s
            or after_end - after_start < min_window_s
        ):
            return None

        samples = self._samples[channel]
        b = [v for t, v in samples if before_start <= t <= before_end]
        a = [v for t, v in samples if after_start <= t <= after_end]
        if len(b) < self._MIN_SAMPLES or len(a) < self._MIN_SAMPLES:
            return None
        return Toggle(
            p.device_id, p.ts, p.is_on, ((channel, fmean(b)),), ((channel, fmean(a)),), now
        )

    # -- edges -----------------------------------------------------------------

    def on_edge(self, edge: Edge, discover: bool = True) -> str | None:
        """Return the device id the edge was attributed to, if any.

        An edge that merged several channels is first offered to state devices
        with a known footprint, each of which takes only its own channels; what
        is left goes through the normal attribution below. If something is left
        and nobody explains it, the result is ``None`` even though part of the
        edge was claimed, so the log keeps showing it as unexplained.
        """
        self._t_first = edge.ts if self._t_first is None else min(self._t_first, edge.ts)
        self._t_last = max(self._t_last, edge.ts)
        claimant: str | None = None
        if len(edge.parts) > 1:
            for dev in self._state.values():
                claimed = dev.claim_parts(edge)
                if not claimed:
                    continue
                claimant = claimant or dev.spec.device_id
                rest = tuple((c, p) for c, p in edge.parts if c not in claimed)
                if not rest or abs(sum(p for _, p in rest)) < self._MIN_REMAINDER_W:
                    return claimant
                edge = Edge(edge.ts, sum(p for _, p in rest), edge.seen_ts, rest)
        return self._attribute(edge, discover)

    def _attribute(self, edge: Edge, discover: bool) -> str | None:
        """Return the device id the edge was attributed to, if any.

        With ``discover=False`` an unexplained edge is not added to the
        discovery inbox. Edges from evidence channels (a solar inverter) use
        this: a cloud steps the inverter just like a load would, so they may
        only confirm a device whose power the user declared.
        """
        claims = [
            (err, dev)
            for dev in self._state.values()
            if (err := dev.offer(edge)) is not None
        ]

        # (device, new level, relative error, restarts a run whose fall was missed)
        hits: list[tuple[LatentDevice, float, float, bool]] = []
        for dev in self._latent.values():
            m = dev.match(edge.delta_w)
            if m:
                hits.append((dev, m[0], m[1], False))
            elif (r := dev.restart_match(edge.delta_w, edge.ts)) is not None:
                hits.append((dev, r[0], r[1], True))
        closing = [h for h in hits if h[1] == 0.0 and not h[3]]

        if claims:
            err, state_dev = min(claims, key=lambda c: c[0])
            # An edge that ends a run we know is open is strong evidence; a state
            # device takes it only when its own match is clearly better.
            rival = min(closing, key=lambda h: h[2], default=None)
            if rival is None or err <= rival[2] * self._CLOSE_BONUS:
                return state_dev.spec.device_id
            hits = [rival]

        if hits:
            dev, level, _, restart = min(hits, key=lambda h: h[2])
            if restart:
                dev.restart(level, edge.ts)
            else:
                dev.apply(level, edge.ts)
            return dev.spec.device_id

        if discover:
            self.discovery.add(edge)
        return None

    # -- consistency with the whole home ---------------------------------------------

    def check_coherence(self, ts: float, house_w: float | None) -> list[str]:
        """End inferred runs the whole-home power says cannot be going on.

        Edges can be missed (a fall that merged with another load, or was claimed by
        a state device), and an inferred device with no entity would then stay "on"
        for hours, inflating energy and zeroing the untracked power. Two things end
        such a run: the home drawing far less than the running inferred devices
        claim (a load cannot exceed the total), and a run lasting far longer than
        any this device has finished. Returns the ids of the devices closed.
        """
        closed: list[str] = []
        if house_w is None:
            return closed
        for dev in self._latent.values():
            limit = dev.run_limit_s()
            if dev.is_on and dev.since is not None and limit and ts - dev.since > limit:
                dev.force_close()
                closed.append(dev.spec.device_id)

        running = sorted(
            (d for d in self._latent.values() if d.is_on), key=lambda d: -d.level_w
        )
        if running and house_w < self._INCOHERENT_FRACTION * sum(d.level_w for d in running):
            if self._incoherent_since is None:
                self._incoherent_since = ts
            if ts - self._incoherent_since >= self._INCOHERENT_S:
                for dev in running:
                    if house_w >= self._INCOHERENT_FRACTION * sum(d.level_w for d in running if d.is_on):
                        break
                    dev.force_close()
                    closed.append(dev.spec.device_id)
                self._incoherent_since = None
        else:
            self._incoherent_since = None
        return closed

    # -- discovery, specs, estimates -----------------------------------------------

    # Acting without asking hides a load from the inbox for good, so only overwhelming
    # evidence qualifies; everything weaker is offered to the user as a suggestion.
    _ADOPT_MIN_COUNT = 20
    _ADOPT_MIN_LIFT = 6.0
    _ADOPT_MIN_SHARE = 0.7
    _SUGGEST_MIN_LIFT = 2.5
    _MAX_OWN_LEVEL_RATIO = 3.0

    def candidates(self, min_count: int = 2) -> list[Candidate]:
        """Recurring unexplained loads, each with the state device it most likely
        belongs to (if one is on far more often than chance when it appears).

        A load that clearly belongs to a running device (its compressor cycling,
        its heater) is handed to that device and leaves the list, so the inbox
        only holds things that are really new.
        """
        table = self._slot_table()
        found = [self._with_association(c, table) for c in self.discovery.candidates(min_count)]
        adopted = [
            c
            for c in found
            if c.likely_device
            and c.count >= self._ADOPT_MIN_COUNT
            and c.lift >= self._ADOPT_MIN_LIFT
            and c.share_on >= self._ADOPT_MIN_SHARE
        ]
        if not adopted:
            return found
        for c in adopted:
            self.add_cycle_level(c.likely_device, c.level_w)
        return [self._with_association(c, table) for c in self.discovery.candidates(min_count)]

    def add_cycle_level(self, device_id: str, level_w: float) -> None:
        """The steps of this size are the device's own cycling while it is on."""
        self._state[device_id].cycle_levels.add(level_w)
        self.discovery.consume(level_w)

    def _slot_table(self):
        """For every state device, how long it was on in each hour of the span.

        Built in one pass per device so that judging each candidate costs only
        a walk over the hour slots, not over every on/off interval.
        """
        if self._t_first is None:
            return None
        first = self._t_first - (self._t_first % 3600)
        n = int((self._t_last - first) // 3600) + 1
        hours = [hour_of_day(first + i * 3600 + 1800) for i in range(n)]
        table = {}
        for device_id in self._state:
            intervals = list(self._on_intervals[device_id])
            if device_id in self._on_since:
                intervals.append((self._on_since[device_id], self._t_last))
            if not intervals:
                continue
            on = [0.0] * n
            for a, b in intervals:
                for i in range(max(0, int((a - first) // 3600)), min(n - 1, int((b - first) // 3600)) + 1):
                    slot = first + i * 3600
                    on[i] += max(0.0, min(b, slot + 3600) - max(a, slot))
            table[device_id] = (on, [a for a, _ in intervals], [b for _, b in intervals])
        return hours, table

    def _with_association(self, candidate: Candidate, table) -> Candidate:
        if table is None or not candidate.starts or candidate.count < 5:
            return candidate
        slot_hours, per_device = table
        wanted = {hour_of_day(t) for t in candidate.starts}
        slots = [i for i, h in enumerate(slot_hours) if h in wanted]
        if not slots:
            return candidate
        best: tuple[str, float, float] | None = None
        for device_id, (on, starts, ends) in per_device.items():
            known = self._state[device_id].learned_w
            if known and candidate.level_w > self._MAX_OWN_LEVEL_RATIO * known:
                continue  # a 300 W appliance does not cycle a 1.3 kW load
            # how often is it on at these hours of the day, over the whole span?
            base = sum(on[i] for i in slots) / (3600.0 * len(slots))
            if base < 0.02:
                continue
            hits = 0
            for t in candidate.starts:
                k = bisect_right(starts, t) - 1
                hits += k >= 0 and ends[k] >= t
            share = hits / len(candidate.starts)
            lift = share / base
            if best is None or lift > best[1]:
                best = (device_id, lift, share)
        if best and best[1] >= self._SUGGEST_MIN_LIFT and best[2] >= 0.3:
            return replace(
                candidate,
                likely_device=best[0],
                lift=round(best[1], 1),
                share_on=round(best[2], 2),
            )
        return candidate

    def assign_candidate(self, device_id: str, level_w: float) -> None:
        """Give a waiting latent device the power level the user picked."""
        self._latent[device_id].set_levels((level_w,))
        self.discovery.consume(level_w)

    def spec(self, device_id: str) -> DeviceSpec:
        """Current spec of a device (reflects pins and assigned candidates)."""
        dev = self._latent.get(device_id) or self._state.get(device_id)
        if dev is None:
            raise KeyError(device_id)
        return dev.spec

    def is_latent(self, device_id: str) -> bool:
        return device_id in self._latent

    def state_device(self, device_id: str):
        """The learning model of a state device, or None."""
        return self._state.get(device_id)

    def provisional_w(self, device_id: str) -> float | None:
        dev = self._state.get(device_id)
        return None if dev is None else dev.provisional_w

    def pin(self, device_id: str) -> None:
        """Freeze a state device's learned power."""
        self._state[device_id].pin()

    def describe(self) -> dict:
        """Everything the engine currently believes, for diagnostics."""
        return {
            "state_devices": {k: v.describe() for k, v in self._state.items()},
            "latent_devices": {
                k: {
                    "levels_w": list(v.spec.power_levels_w),
                    "on": v.is_on,
                    "finished_runs": len(v.runs),
                    "forced_closes": v.forced_closes,
                    "run_limit_min": None if v.run_limit_s() is None else round(v.run_limit_s() / 60),
                }
                for k, v in self._latent.items()
            },
            "channels_with_samples": sorted(self._samples),
            "channel_timing": {
                c: dict(zip(("before_s", "after_s", "skip_s", "min_window_s"), v))
                for c, v in self._channel_timing.items()
            },
            "pending_toggles": len(self._pending),
            "discovery_candidates": len(self.discovery.candidates(1)),
        }

    def snapshot(self) -> dict[str, Estimate]:
        out: dict[str, Estimate] = {}
        for lat in self._latent.values():
            waiting = lat.awaiting_discovery
            out[lat.spec.device_id] = Estimate(
                lat.spec.device_id,
                lat.is_on,
                lat.level_w,
                0.0 if waiting else 0.8,
                "awaiting_discovery" if waiting else "inferred_from_edges",
            )
        for dev in self._state.values():
            out[dev.spec.device_id] = Estimate(
                dev.spec.device_id,
                dev.is_on,
                dev.power_w,
                dev.confidence,
                dev.source,
                dev.channel_powers,
            )
        return out
