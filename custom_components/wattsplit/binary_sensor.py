"""Running/not-running for appliances that have no entity of their own."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import WattSplitConfigEntry, WattSplitCoordinator
from .entity import ApplianceEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WattSplitConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    for subentry in entry.subentries.values():
        if coordinator.specs[subentry.subentry_id].is_latent:
            async_add_entities(
                [RunningBinarySensor(coordinator, subentry)],
                config_subentry_id=subentry.subentry_id,
            )


class RunningBinarySensor(ApplianceEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(self, coordinator: WattSplitCoordinator, subentry: Any) -> None:
        super().__init__(coordinator, subentry, "running")

    @property
    def is_on(self) -> bool | None:
        est = self.estimate
        return None if est is None else est.is_on

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        est = self.estimate
        return {} if est is None else {"source": est.source}
