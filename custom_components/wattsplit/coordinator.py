"""Samples the inputs on a timer and drives the engine."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.event import (
    EventStateChangedData,
    async_track_state_change_event,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ENERGY_ENTITY,
    CONF_EVIDENCE,
    CONF_MIN_STEP,
    CONF_POWER_ENTITY,
    CONF_POWER_LEVELS,
    CONF_POWER_MODE,
    CONF_SAMPLE_INTERVAL,
    CONF_STATE_ENTITY,
    DEFAULT_MIN_STEP_W,
    DEFAULT_SAMPLE_INTERVAL_S,
    DOMAIN,
    HISTORY_DAYS,
    LOG_RETENTION_DAYS,
    MAX_ENERGY_STEP_S,
    NOISE_K,
    RETIME_CHANGE,
    STALE_AFTER_S,
)
from .engine import (
    Candidate,
    Disaggregator,
    EdgeLog,
    Estimate,
    GlitchFilter,
    MultiChannelDetector,
    Plan,
    PowerMode,
    RateTracker,
    Timing,
    Validator,
    channel_values,
    choose_plan,
    consumption_w,
    replay,
    timing_for,
)
from .helpers import (
    energy_wh,
    format_levels,
    is_on,
    power_w,
    sources_from_entry,
    spec_from_subentry,
)

_LOGGER = logging.getLogger(__name__)

type WattSplitConfigEntry = ConfigEntry[WattSplitCoordinator]


@dataclass
class WattSplitData:
    available: bool
    house_w: float | None = None
    measured_w: float = 0.0
    untracked_w: float = 0.0
    dt_s: float = 0.0
    estimates: dict[str, Estimate] = field(default_factory=dict)
    candidates: list[Candidate] = field(default_factory=list)
    detection: dict[str, Any] = field(default_factory=dict)
    validation: dict[str, Any] = field(default_factory=dict)
    cross_checks: dict[str, Any] = field(default_factory=dict)


class WattSplitCoordinator(DataUpdateCoordinator[WattSplitData]):
    config_entry: WattSplitConfigEntry

    def __init__(self, hass: HomeAssistant, entry: WattSplitConfigEntry) -> None:
        interval = float(
            entry.options.get(CONF_SAMPLE_INTERVAL, DEFAULT_SAMPLE_INTERVAL_S)
        )
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=interval),
        )
        self._interval = interval
        self._min_step = float(entry.options.get(CONF_MIN_STEP, DEFAULT_MIN_STEP_W))
        self.sources = sources_from_entry(entry)
        self._evidence_option = entry.options.get(CONF_EVIDENCE)
        self.specs = {
            sub.subentry_id: spec_from_subentry(sub)
            for sub in entry.subentries.values()
        }
        self._state_entities: dict[str, list[str]] = {}  # entity_id -> device ids
        self._measured: dict[str, str] = {}  # device id -> power entity
        for sub in entry.subentries.values():
            if state_entity := sub.data.get(CONF_STATE_ENTITY):
                if not sub.data.get(CONF_POWER_ENTITY):
                    self._state_entities.setdefault(state_entity, []).append(
                        sub.subentry_id
                    )
            if power_entity := sub.data.get(CONF_POWER_ENTITY):
                self._measured[sub.subentry_id] = power_entity

        # An appliance with both an on/off entity and a power sensor is ground
        # truth: the engine is scored on it as if the power sensor did not exist.
        self._validators: dict[str, Validator] = {}
        self._validator_entities: dict[str, list[str]] = {}
        for sub in entry.subentries.values():
            state_entity = sub.data.get(CONF_STATE_ENTITY)
            if state_entity and sub.data.get(CONF_POWER_ENTITY):
                self._validators[sub.subentry_id] = Validator(sub.subentry_id, sub.title)
                self._validator_entities.setdefault(state_entity, []).append(
                    sub.subentry_id
                )

        # An appliance's own cumulative energy counter is a clean, interference-free
        # measurement, so it is used as a starting point and a cross-check.
        self._counter_entities: dict[str, list[str]] = {}
        self._counter_of: dict[str, str] = {}
        for sub in entry.subentries.values():
            counter = sub.data.get(CONF_ENERGY_ENTITY)
            if (
                counter
                and sub.data.get(CONF_STATE_ENTITY)
                and not sub.data.get(CONF_POWER_ENTITY)
            ):
                self._counter_entities.setdefault(counter, []).append(sub.subentry_id)
                self._counter_of[sub.subentry_id] = counter

        # How often each input really reports, measured from last_reported.
        self._rates: dict[str, RateTracker] = {}
        self._reported_at: dict[str, float] = {}
        self._house_filter = GlitchFilter()
        self._house_good: float | None = None

        self._plan: Plan | None = None
        self._chan_timing: dict[str, Timing] = {}
        self.detector = MultiChannelDetector(["total"], dt_s=interval)
        self.evidence_detector: MultiChannelDetector | None = None
        self.diss = Disaggregator(list(self.specs.values()))
        self.log: EdgeLog | None = None
        self._last_ts: float | None = None
        self._candidates: list[Candidate] = []
        self._candidates_dirty = True
        self._unsub_states: Any = None

    async def async_setup(self) -> None:
        """Open the log and rebuild learned state by replaying it."""
        path = self.hass.config.path(
            ".storage", f"{DOMAIN}_{self.config_entry.entry_id}.db"
        )
        self.log = await self.hass.async_add_executor_job(EdgeLog, path)
        cutoff = time.time() - LOG_RETENTION_DAYS * 86400
        await self.hass.async_add_executor_job(self.log.prune, cutoff)
        try:
            await self._async_bootstrap_history()
        except Exception:  # noqa: BLE001 - history is a bonus, it must never stop setup
            _LOGGER.warning("Could not learn from the recorder history", exc_info=True)
        # Edges seen on generation / battery sensors never enter the discovery
        # inbox (a cloud steps an inverter like a load would).
        restricted = frozenset(
            (
                *self.sources.generation,
                *self.sources.battery,
                *(self._evidence_option or ()),
            )
        )
        specs = list(self.specs.values())
        self.diss = await self.hass.async_add_executor_job(
            lambda: replay(self.log, specs, restricted_channels=restricted)
        )
        self.diss.go_live()
        for entity_id, device_ids in self._state_entities.items():
            on = is_on(self.hass.states.get(entity_id))
            if on is not None:
                for device_id in device_ids:
                    self.diss.sync_state(device_id, on, time.time())
        for entity_id, device_ids in self._validator_entities.items():
            on = is_on(self.hass.states.get(entity_id))
            if on is not None:
                for device_id in device_ids:
                    self._validators[device_id].sync_state(on)
        await self._async_baseline_counters()
        watched = list(
            {*self._state_entities, *self._validator_entities, *self._counter_entities}
        )
        if watched:
            self._unsub_states = async_track_state_change_event(
                self.hass, watched, self._async_on_state_event
            )

    async def async_shutdown(self) -> None:
        await super().async_shutdown()
        if self._unsub_states:
            self._unsub_states()
            self._unsub_states = None
        if self.log is not None:
            await self.hass.async_add_executor_job(self.log.close)
            self.log = None

    # -- inputs ---------------------------------------------------------------------

    def _tracked_entities(self) -> list[str]:
        return list(
            dict.fromkeys(
                [
                    *self.sources.everything,
                    *(self._evidence_option or ()),
                    *self._measured.values(),
                ]
            )
        )

    def _observe_rates(self) -> None:
        for entity_id in self._tracked_entities():
            state = self.hass.states.get(entity_id)
            if state is None or state.last_reported is None:
                continue
            reported = state.last_reported.timestamp()
            if self._reported_at.get(entity_id) != reported:
                self._reported_at[entity_id] = reported
                self._rates.setdefault(entity_id, RateTracker()).observe(reported)

    def _period(self, entity_id: str) -> float | None:
        tracker = self._rates.get(entity_id)
        return tracker.period_s if tracker else None

    def _read(self, entity_id: str, now: float) -> float | None:
        """A power reading in W, or None if missing, non-numeric or stale."""
        state = self.hass.states.get(entity_id)
        if state is None or now - self._reported_at.get(entity_id, now) > STALE_AFTER_S:
            return None
        return power_w(state)

    def _read_measured_w(self, now: float) -> float:
        total = 0.0
        for entity_id in self._measured.values():
            value = self._read(entity_id, now)
            if value is not None and value > 0:
                total += value
        return total

    # -- timing ---------------------------------------------------------------------

    def _channel_timing(self, plan: Plan) -> dict[str, Timing]:
        """Windows for every channel, from that channel's own reporting rate."""
        wanted = {c: list(plan.sensors_of(c)) for c in plan.detect}
        wanted.update({e: [e] for e in plan.evidence})
        out = {}
        for channel, sensors in wanted.items():
            periods = [p for s in sensors if (p := self._period(s)) is not None]
            out[channel] = timing_for(max(periods) if periods else None, self._interval)
        return out

    def _retime(self, plan: Plan) -> None:
        """(Re)build the detectors when the plan or the measured rates change."""
        new = self._channel_timing(plan)
        old_plan, old = self._plan, self._chan_timing

        def moved(names: tuple[str, ...]) -> bool:
            return any(
                c in old
                and abs(new[c].settle_s - old[c].settle_s)
                > RETIME_CHANGE * old[c].settle_s
                for c in names
            )

        for channel, tm in new.items():
            before = old.get(channel)
            if (
                before is None
                or abs(tm.window_s - before.window_s) > RETIME_CHANGE * before.window_s
            ):
                self.diss.set_timing(
                    tm.window_s, tm.window_s, tm.skip_s, tm.min_window_s, channel=channel
                )
                for validator in self._validators.values():
                    validator.set_timing(
                        tm.window_s, tm.window_s, tm.skip_s, tm.min_window_s, channel=channel
                    )

        rebuild_main = (
            old_plan is None or plan.detect != old_plan.detect or moved(plan.detect)
        )
        rebuild_evidence = (
            old_plan is None
            or plan.evidence != old_plan.evidence
            or moved(plan.evidence)
        )
        if rebuild_main:
            self.detector = self._build_detector(plan.detect, new)
        if rebuild_evidence:
            self.evidence_detector = (
                self._build_detector(plan.evidence, new) if plan.evidence else None
            )
        if rebuild_main or rebuild_evidence:
            _LOGGER.debug("Detecting on %s, evidence %s", plan.detect, plan.evidence)
        self._plan, self._chan_timing = plan, {**old, **new}

    def _build_detector(
        self, channels: tuple[str, ...], timing: dict[str, Timing]
    ) -> MultiChannelDetector:
        return MultiChannelDetector(
            list(channels),
            merge_s=max(timing[c].merge_s for c in channels) if len(channels) > 1 else 0.0,
            per_channel={
                c: {"settle_s": timing[c].settle_s, "ref_s": timing[c].ref_s}
                for c in channels
            },
            dt_s=self._interval,
            min_step_w=self._min_step,
            noise_k=NOISE_K,
        )

    # -- state events ------------------------------------------------------------------

    @callback
    def _async_on_state_event(self, event: Event[EventStateChangedData]) -> None:
        entity_id = event.data["entity_id"]
        if entity_id in self._counter_entities:
            self._on_counter_event(entity_id, event)
            return
        now_on = is_on(event.data["new_state"])
        was_on = is_on(event.data["old_state"])
        if now_on is None:
            return
        ts = event.time_fired.timestamp()
        for device_id in self._state_entities.get(entity_id, []):
            if was_on is None or was_on == now_on:
                self.diss.sync_state(device_id, now_on)
                continue
            self.diss.on_state(device_id, now_on, ts)
            if self.log is not None:
                self.config_entry.async_create_background_task(
                    self.hass,
                    self._async_log_state(ts, device_id, now_on),
                    f"{DOMAIN}_log_state",
                )
        for device_id in self._validator_entities.get(entity_id, []):
            if was_on is None or was_on == now_on:
                self._validators[device_id].sync_state(now_on)
            else:
                self._validators[device_id].on_state(now_on, ts)

    def _on_counter_event(self, entity_id: str, event: Event[EventStateChangedData]) -> None:
        wh = energy_wh(event.data["new_state"])
        if wh is None:
            return
        ts = event.time_fired.timestamp()
        for device_id in self._counter_entities[entity_id]:
            self.diss.on_counter(device_id, ts, wh)
            if self.log is not None:
                self.config_entry.async_create_background_task(
                    self.hass,
                    self._async_log_counter(ts, device_id, wh),
                    f"{DOMAIN}_log_counter",
                )

    async def _async_log_counter(self, ts: float, device_id: str, wh: float) -> None:
        if self.log is not None:
            await self.hass.async_add_executor_job(self.log.add_counter, ts, device_id, wh)

    async def _async_bootstrap_history(self) -> None:
        """Start from the recorder's history instead of from nothing.

        For every appliance with an energy counter that the log has never seen,
        copy the last ``HISTORY_DAYS`` of its state changes and counter readings
        into the log, so the very first replay already has an estimate.
        """
        if self.log is None or not self._counter_of or "recorder" not in self.hass.config.components:
            return
        log = self.log
        known = {device for _, device, _ in await self.hass.async_add_executor_job(log.counters)}
        wanted = {
            d: (self._state_entity_of(d), counter)
            for d, counter in self._counter_of.items()
            if d not in known
        }
        if not wanted:
            return
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.history import get_significant_states

        end = dt_util.utcnow()
        start = end - timedelta(days=HISTORY_DAYS)
        entity_ids = sorted({e for pair in wanted.values() for e in pair if e})
        try:
            history = await get_instance(self.hass).async_add_executor_job(
                get_significant_states, self.hass, start, end, entity_ids,
                None, True, False, False, False,
            )
        except Exception:  # noqa: BLE001 - history is a bonus, never fatal
            _LOGGER.debug("Could not read the recorder history", exc_info=True)
            return

        rows_state: list[tuple[float, str, bool]] = []
        rows_counter: list[tuple[float, str, float]] = []
        for device_id, (state_entity, counter_entity) in wanted.items():
            previous = None
            for state in history.get(state_entity, []):
                on = is_on(state)
                if on is None or on == previous:
                    continue
                previous = on
                rows_state.append((state.last_updated.timestamp(), device_id, on))
            for state in history.get(counter_entity, []):
                wh = energy_wh(state)
                if wh is not None:
                    rows_counter.append((state.last_updated.timestamp(), device_id, wh))

        def write() -> None:
            for ts, device_id, on in sorted(rows_state):
                log.add_state(ts, device_id, on)
            for ts, device_id, wh in sorted(rows_counter):
                log.add_counter(ts, device_id, wh)

        await self.hass.async_add_executor_job(write)
        _LOGGER.debug(
            "Bootstrapped %d state changes and %d counter readings from history",
            len(rows_state), len(rows_counter),
        )

    def _state_entity_of(self, device_id: str) -> str | None:
        subentry = self.config_entry.subentries.get(device_id)
        return subentry.data.get(CONF_STATE_ENTITY) if subentry else None

    async def _async_baseline_counters(self) -> None:
        """Put each counter's current reading on the timeline, once."""
        for counter_entity, device_ids in self._counter_entities.items():
            state = self.hass.states.get(counter_entity)
            wh = energy_wh(state)
            if wh is None:
                continue
            ts = state.last_updated.timestamp()
            for device_id in device_ids:
                self.diss.on_counter(device_id, ts, wh)  # ignored if already known
                if self.log is not None:
                    later = await self.hass.async_add_executor_job(self.log.counters, ts)
                    if not any(d == device_id for _, d, _ in later):
                        await self.hass.async_add_executor_job(
                            self.log.add_counter, ts, device_id, wh
                        )

    async def _async_log_state(self, ts: float, device_id: str, on: bool) -> None:
        if self.log is not None:
            await self.hass.async_add_executor_job(self.log.add_state, ts, device_id, on)

    # -- the tick ---------------------------------------------------------------------

    async def _async_update_data(self) -> WattSplitData:
        now = time.time()
        dt = min(now - self._last_ts, MAX_ENERGY_STEP_S) if self._last_ts else 0.0
        self._last_ts = now
        src = self.sources

        self._observe_rates()
        plan = choose_plan(src, self._period, self._evidence_option)
        self._retime(plan)

        # A derived house sensor may read exactly 0 W when one of its inputs
        # drops out. Detection ignores that reading; the display keeps the last
        # good value instead of flashing to zero.
        house_raw = self._read(src.house, now) if src.house else None
        house_clean = self._house_filter.filter(now, house_raw) if src.house else None
        if house_clean is not None:
            self._house_good = house_clean

        def detect_read(sensor: str) -> float | None:
            return house_clean if sensor == src.house else self._read(sensor, now)

        def show_read(sensor: str) -> float | None:
            if sensor == src.house:
                return None if house_raw is None else self._house_good
            return self._read(sensor, now)

        house = consumption_w(src, show_read)
        readings = channel_values(plan, src, detect_read)
        measured = self._read_measured_w(now)
        if len(plan.detect) == 1:  # a metered appliance is inside the one signal
            readings = {c: v - measured for c, v in readings.items()}
        if house is None or not readings:
            self.detector.reset()
            return WattSplitData(available=False, dt_s=dt)

        evidence = {e: v for e in plan.evidence if (v := self._read(e, now)) is not None}
        for toggle in self.diss.on_sample(now, {**readings, **evidence}):
            if self.log is not None:
                await self.hass.async_add_executor_job(self.log.add_toggle, toggle)
        if self._validators:
            # The shadow engine must still see the metered appliances' consumption.
            unsubtracted = (
                {ch: v + measured for ch, v in readings.items()}
                if len(plan.detect) == 1
                else readings
            )
            for device_id, validator in self._validators.items():
                truth = self._read(self._measured[device_id], now)
                validator.on_sample(now, {**unsubtracted, **evidence}, truth)

        edge = self.detector.update(now, readings)
        if edge is not None:
            device_id = self.diss.on_edge(edge)
            if device_id is None:
                self._candidates_dirty = True
            if self.log is not None:
                await self.hass.async_add_executor_job(self.log.add_edge, edge, device_id)
        if self.evidence_detector is not None and evidence:
            ev_edge = self.evidence_detector.update(now, evidence)
            if ev_edge is not None:
                device_id = self.diss.on_edge(ev_edge, discover=False)
                if self.log is not None:
                    await self.hass.async_add_executor_job(
                        self.log.add_edge, ev_edge, device_id
                    )

        if self._candidates_dirty:
            self._candidates = self.diss.candidates()
            self._candidates_dirty = False

        estimates = self.diss.snapshot()
        tracked = measured + sum(e.power_w for e in estimates.values())
        return WattSplitData(
            available=True,
            house_w=house,
            measured_w=measured,
            untracked_w=max(0.0, house - tracked),
            dt_s=dt,
            estimates=estimates,
            candidates=self._candidates,
            detection=self._describe_detection(plan),
            validation=self._validation_report(),
            cross_checks=self._cross_checks(),
        )

    def _cross_checks(self) -> dict[str, Any]:
        """Whole-house channels versus the appliance's own energy counter."""
        subentries = self.config_entry.subentries
        return {
            subentries[device_id].title: check
            for device_id, check in self.diss.cross_checks().items()
            if device_id in subentries
        }

    def _validation_report(self) -> dict[str, Any]:
        """Per metered appliance: what the engine would have said without the sensor."""
        subentries = self.config_entry.subentries
        return {
            subentries[device_id].title: validator.report()
            for device_id, validator in self._validators.items()
            if device_id in subentries
        }

    def _describe_detection(self, plan: Plan) -> dict[str, Any]:
        thresholds = dict(self.detector.thresholds())
        if self.evidence_detector is not None:
            thresholds.update(self.evidence_detector.thresholds())
        return {
            "mode": plan.kind,
            "detecting_on": list(plan.detect),
            "evidence_only": list(plan.evidence),
            "update_period_s": {
                e: round(p) for e in self._tracked_entities() if (p := self._period(e))
            },
            "min_step_w": {c: round(t) for c, t in thresholds.items()},
            "shortest_run_s": {
                c: round(tm.shortest_run_s) for c, tm in self._chan_timing.items()
            },
        }

    # -- actions (called from entity services) ---------------------------------------------

    def _persist(self, device_id: str, **changes: Any) -> None:
        entry = self.config_entry
        subentry = entry.subentries[device_id]
        self.hass.config_entries.async_update_subentry(
            entry, subentry, data={**subentry.data, **changes}
        )  # the entry's update listener reloads and replays

    def pin(self, device_id: str) -> None:
        spec = self.specs.get(device_id)
        if spec is None or spec.has_power or spec.is_latent:
            raise ServiceValidationError(
                "Only appliances with an on/off entity and no power sensor can be pinned"
            )
        self.diss.pin(device_id)
        spec = self.diss.spec(device_id)
        if not spec.power_levels_w:
            raise ServiceValidationError("Nothing learned yet; nothing to pin")
        self._persist(
            device_id,
            **{
                CONF_POWER_MODE: PowerMode.PINNED.value,
                CONF_POWER_LEVELS: format_levels(spec.power_levels_w),
            },
        )

    def assign_candidate(self, device_id: str, level_w: float) -> None:
        if not self.diss.is_latent(device_id):
            raise ServiceValidationError(
                "Candidates can only be assigned to latent appliances"
            )
        self.diss.assign_candidate(device_id, level_w)
        self._persist(
            device_id,
            **{
                CONF_POWER_MODE: PowerMode.KNOWN.value,
                CONF_POWER_LEVELS: format_levels((level_w,)),
            },
        )

    # -- diagnostics ------------------------------------------------------------------------

    def diagnostics(self) -> dict[str, Any]:
        """Everything needed to understand what the engine sees and believes."""
        plan = self._plan
        return {
            "sources": {
                "grid": list(self.sources.grid),
                "load": list(self.sources.load),
                "generation": list(self.sources.generation),
                "battery": list(self.sources.battery),
                "house": self.sources.house,
                "invert_grid": self.sources.invert_grid,
            },
            "plan": None
            if plan is None
            else {
                "kind": plan.kind,
                "detect": list(plan.detect),
                "members": {c: list(s) for c, s in plan.members},
                "evidence": list(plan.evidence),
            },
            "update_period_s": {e: self._period(e) for e in self._tracked_entities()},
            "timing": {
                c: {
                    "settle_s": tm.settle_s,
                    "window_s": tm.window_s,
                    "skip_s": tm.skip_s,
                    "min_window_s": tm.min_window_s,
                    "shortest_run_s": tm.shortest_run_s,
                }
                for c, tm in self._chan_timing.items()
            },
            "engine": self.diss.describe(),
            "validation": self._validation_report(),
            "cross_checks": self._cross_checks(),
        }
