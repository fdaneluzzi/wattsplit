"""Shared entity bases."""

from __future__ import annotations

from homeassistant.config_entries import ConfigSubentry
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.device import async_entity_id_to_device
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_STATE_ENTITY, DOMAIN
from .coordinator import WattSplitCoordinator
from .engine import Estimate


class _ActionStubs:
    """Entity services are registered on the whole platform, so every entity
    must answer them; only the appliance power sensor does anything."""

    async def async_pin(self) -> None:
        raise ServiceValidationError("This entity does not support pinning")

    async def async_assign_candidate(self, level_w: float) -> None:
        raise ServiceValidationError("This entity does not support candidates")


class GlobalEntity(_ActionStubs, CoordinatorEntity[WattSplitCoordinator]):
    """Whole-house entities, grouped under one WattSplit device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: WattSplitCoordinator, key: str) -> None:
        super().__init__(coordinator)
        entry_id = coordinator.config_entry.entry_id
        self._attr_unique_id = f"{entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)}, name="WattSplit"
        )

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data.available


class ApplianceEntity(_ActionStubs, CoordinatorEntity[WattSplitCoordinator]):
    """An entity that belongs to one appliance subentry.

    Appliances backed by an existing entity are attached to that entity's
    device, so the estimates show up on the device page the user already has.
    """

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: WattSplitCoordinator, subentry: ConfigSubentry, key: str
    ) -> None:
        super().__init__(coordinator)
        self.device_id = subentry.subentry_id
        self._attr_unique_id = f"{subentry.subentry_id}_{key}"
        self._attr_translation_key = key

        linked = None
        if state_entity := subentry.data.get(CONF_STATE_ENTITY):
            linked = async_entity_id_to_device(coordinator.hass, state_entity)
        if linked is not None:
            self.device_entry = linked
        else:
            self._attr_device_info = DeviceInfo(
                identifiers={(DOMAIN, subentry.subentry_id)}, name=subentry.title
            )

    @property
    def estimate(self) -> Estimate | None:
        return self.coordinator.data.estimates.get(self.device_id)

    @property
    def available(self) -> bool:
        return (
            super().available
            and self.coordinator.data.available
            and self.estimate is not None
        )
