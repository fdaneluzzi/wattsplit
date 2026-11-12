"""Power and energy sensors."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import voluptuous as vol
from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import ENERGY_PUBLISH_S
from .coordinator import WattSplitConfigEntry, WattSplitCoordinator, WattSplitData
from .entity import ApplianceEntity, GlobalEntity

# Labels of Candidate.duration_counts (see engine.discovery.DURATION_EDGES_S).
_LENGTH_LABELS = ("under_1_min", "1_to_5_min", "5_to_15_min", "15_to_60_min", "1_to_3_h", "over_3_h")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WattSplitConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data

    async_add_entities(
        [
            HousePowerSensor(coordinator),
            UntrackedPowerSensor(coordinator),
            UntrackedEnergySensor(coordinator),
            CandidatesSensor(coordinator),
            AccuracyCheckSensor(coordinator),
        ]
    )

    for subentry in entry.subentries.values():
        spec = coordinator.specs[subentry.subentry_id]
        if spec.has_power:
            continue  # measured: nothing to estimate
        async_add_entities(
            [
                AppliancePowerSensor(coordinator, subentry),
                ApplianceEnergySensor(coordinator, subentry),
            ],
            config_subentry_id=subentry.subentry_id,
        )

    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service("pin", {}, "async_pin")
    platform.async_register_entity_service(
        "assign_candidate",
        {vol.Required("level_w"): vol.All(vol.Coerce(float), vol.Range(min=50))},
        "async_assign_candidate",
    )


class _EnergyMixin(RestoreSensor):
    """Integrates a power value (W) into kWh and survives restarts."""

    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_suggested_display_precision = 3

    _kwh = 0.0
    _last_publish = 0.0
    _last_available: bool | None = None
    _last_power: float | None = None

    def _power_w(self) -> float | None:  # pragma: no cover - overridden
        raise NotImplementedError

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_sensor_data()) is not None:
            try:
                self._kwh = float(last.native_value)
            except (TypeError, ValueError):
                self._kwh = 0.0

    @property
    def native_value(self) -> float:
        return round(self._kwh, 6)

    def _handle_coordinator_update(self) -> None:
        data: WattSplitData = self.coordinator.data  # type: ignore[attr-defined]
        power = self._power_w()
        if data.available and power is not None:
            self._kwh += power * data.dt_s / 3_600_000
        # Integrate every tick, but write the state only now and then: a state per
        # tick is ~43 000 rows a day per running appliance in the recorder.
        now = time.monotonic()
        available = self.available  # type: ignore[attr-defined]
        stopped = (power or 0.0) == 0.0 and (self._last_power or 0.0) > 0.0
        if available != self._last_available or stopped or now - self._last_publish >= ENERGY_PUBLISH_S:
            self._last_publish, self._last_available = now, available
            super()._handle_coordinator_update()  # type: ignore[misc]
        self._last_power = power


# -- whole house ------------------------------------------------------------


class _GlobalPower(GlobalEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0
    _value: Callable[[WattSplitData], float | None]

    def __init__(self, coordinator: WattSplitCoordinator, key: str) -> None:
        super().__init__(coordinator, key)

    @property
    def native_value(self) -> float | None:
        value = self._value(self.coordinator.data)
        return None if value is None else round(value, 1)


class HousePowerSensor(_GlobalPower):
    _value = staticmethod(lambda d: d.house_w)

    def __init__(self, coordinator: WattSplitCoordinator) -> None:
        super().__init__(coordinator, "house_power")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """What the engine is currently looking at, and how fast it reports."""
        return self.coordinator.data.detection


class UntrackedPowerSensor(_GlobalPower):
    _value = staticmethod(lambda d: d.untracked_w)

    def __init__(self, coordinator: WattSplitCoordinator) -> None:
        super().__init__(coordinator, "untracked_power")


class UntrackedEnergySensor(_EnergyMixin, GlobalEntity):
    def __init__(self, coordinator: WattSplitCoordinator) -> None:
        super().__init__(coordinator, "untracked_energy")

    def _power_w(self) -> float | None:
        return self.coordinator.data.untracked_w


class CandidatesSensor(GlobalEntity, SensorEntity):
    """Recurring loads nobody explains. They also arrive as items in Settings > Repairs."""

    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    # A long list that changes with every candidate: not worth a recorder row each time.
    _unrecorded_attributes = frozenset({"candidates"})

    def __init__(self, coordinator: WattSplitCoordinator) -> None:
        super().__init__(coordinator, "candidates")

    @property
    def native_value(self) -> int:
        return len(self.coordinator.data.candidates)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "candidates": [
                {
                    "level_w": round(c.level_w),
                    "count": c.count,
                    "days": c.days,
                    "different_hours": len(c.hours),
                    "median_duration_min": round(c.median_duration_s / 60, 1),
                    "duration_range_min": [
                        round(c.duration_p10_s / 60, 1),
                        round(c.duration_p90_s / 60, 1),
                    ],
                    "typical_hour": c.typical_hour,
                    "coherent": c.coherent,
                    "runs_by_hour": list(c.hour_counts),
                    "runs_by_length": dict(zip(_LENGTH_LABELS, c.duration_counts)),
                    "recent": [
                        {
                            "start": dt_util.as_local(dt_util.utc_from_timestamp(start)).isoformat(
                                timespec="seconds"
                            ),
                            "duration_s": round(duration),
                            "level_w": round(level),
                        }
                        for start, duration, level in c.recent
                    ],
                }
                for c in self.coordinator.data.candidates
            ]
        }


class AccuracyCheckSensor(GlobalEntity, SensorEntity):
    """How far the engine is from a real power sensor, on this house.

    Two kinds of ground truth, both reported as a percentage and averaged:
    an appliance with a real power sensor is re-run as if the sensor did not
    exist; an appliance with an energy counter has what the whole-home
    channels say compared with what its own counter says. The state is the mean
    absolute difference in percent; the attributes list each appliance.
    """

    _attr_native_unit_of_measurement = "%"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: WattSplitCoordinator) -> None:
        super().__init__(coordinator, "accuracy_check")

    @property
    def native_value(self) -> float | None:
        data = self.coordinator.data
        errors = [
            abs(r["error_pct"])
            for r in data.validation.values()
            if r.get("error_pct") is not None
        ] + [abs(c["difference_pct"]) for c in data.cross_checks.values()]
        return round(sum(errors) / len(errors), 1) if errors else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        return {"appliances": data.validation, "counter_vs_channels": data.cross_checks}


# -- appliances ---------------------------------------------------------------


class AppliancePowerSensor(ApplianceEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0

    def __init__(self, coordinator: WattSplitCoordinator, subentry: Any) -> None:
        super().__init__(coordinator, subentry, "estimated_power")

    @property
    def native_value(self) -> float | None:
        est = self.estimate
        return None if est is None else round(est.power_w, 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        est = self.estimate
        spec = self.coordinator.diss.spec(self.device_id)
        if est is None:
            return {}
        attrs: dict[str, Any] = {
            "is_on": est.is_on,
            "confidence": round(est.confidence, 2),
            "source": est.source,
            "power_mode": spec.mode.value,
            "power_levels_w": list(spec.power_levels_w),
        }
        if est.channels:
            attrs["channels_w"] = dict(est.channels)
        device = self.coordinator.diss.state_device(self.device_id)
        if device is not None:
            info = device.describe()
            attrs["evidence"] = {
                "counter_intervals": info["counter"]["usable_intervals"],
                "toggles": info["toggles"],
                "own_cycle_levels_w": info["cycle_levels_w"],
            }
        provisional = self.coordinator.diss.provisional_w(self.device_id)
        if provisional is not None and est.source == "learned_from_state":
            attrs["provisional_w"] = round(provisional)
        return attrs

    async def async_pin(self) -> None:
        self.coordinator.pin(self.device_id)

    async def async_assign_candidate(self, level_w: float) -> None:
        self.coordinator.assign_candidate(self.device_id, level_w)


class ApplianceEnergySensor(_EnergyMixin, ApplianceEntity):
    def __init__(self, coordinator: WattSplitCoordinator, subentry: Any) -> None:
        super().__init__(coordinator, subentry, "estimated_energy")

    def _power_w(self) -> float | None:
        est = self.estimate
        return None if est is None else est.power_w
