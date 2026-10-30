"""Reuse what the user already told Home Assistant's Energy dashboard.

The dashboard knows which sensor is the grid power, which is the solar power,
and which appliances have an energy counter. That is exactly the information
WattSplit needs, so it should not have to be typed twice.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import CONF_ENERGY_ENTITY, CONF_STATE_ENTITY, SUBENTRY_APPLIANCE
from .engine.states import pick_counter, pick_state_entity

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class EnergyDevice:
    name: str
    counter: str  # the energy sensor the dashboard uses for it


@dataclass(frozen=True)
class EnergyImport:
    grid: tuple[str, ...] = ()
    generation: tuple[str, ...] = ()
    battery: tuple[str, ...] = ()
    devices: tuple[EnergyDevice, ...] = ()


def _power_sensor(source: dict[str, Any]) -> str | None:
    return source.get("stat_rate") or (source.get("power_config") or {}).get("stat_rate")


async def async_read_energy_dashboard(hass: HomeAssistant) -> EnergyImport | None:
    """What the Energy dashboard is configured with, or None if nothing/unavailable."""
    try:
        from homeassistant.components.energy.data import async_get_manager

        manager = await async_get_manager(hass)
    except Exception:  # noqa: BLE001 - the dashboard is optional, never fatal
        _LOGGER.debug("Energy dashboard preferences are not available", exc_info=True)
        return None
    prefs = manager.data
    if not prefs:
        return None

    grid: list[str] = []
    generation: list[str] = []
    battery: list[str] = []
    for source in prefs.get("energy_sources", []):
        power = _power_sensor(source)
        if not power:
            continue
        kind = source.get("type")
        if kind == "grid":
            grid.append(power)
        elif kind == "solar":
            generation.append(power)
        elif kind == "battery":
            battery.append(power)
    devices = tuple(
        EnergyDevice(d.get("name") or d["stat_consumption"], d["stat_consumption"])
        for d in prefs.get("device_consumption", [])
        if d.get("stat_consumption")
    )
    return EnergyImport(tuple(grid), tuple(generation), tuple(battery), devices)


def appliances_from_dashboard(
    hass: HomeAssistant, entry: ConfigEntry, imported: EnergyImport
) -> tuple[list[ConfigSubentry], list[str]]:
    """Appliances to add for the dashboard's devices, and the names it could not.

    Each device keeps its dashboard name. Its on/off entity is guessed among the
    other entities of the same device, and its counter is the finest one the
    device offers. Devices already present (same counter or same on/off entity)
    are left alone.
    """
    registry = er.async_get(hass)
    taken = {
        v
        for sub in entry.subentries.values()
        for v in (sub.data.get(CONF_ENERGY_ENTITY), sub.data.get(CONF_STATE_ENTITY))
        if v
    }
    new: list[ConfigSubentry] = []
    skipped: list[str] = []
    for device in imported.devices:
        registered = registry.async_get(device.counter)
        if registered is None or registered.device_id is None:
            skipped.append(device.name)
            continue
        siblings = er.async_entries_for_device(registry, registered.device_id)
        described = []
        for sibling in siblings:
            state = hass.states.get(sibling.entity_id)
            described.append(
                (sibling.entity_id, state.state if state else "", dict(state.attributes) if state else {})
            )
        state_entity = pick_state_entity(described)
        counter = pick_counter((s.entity_id for s in siblings), device.counter)
        if state_entity is None:
            skipped.append(device.name)
            continue
        if counter in taken or state_entity in taken or device.counter in taken:
            continue
        taken.update((counter, state_entity, device.counter))
        new.append(
            ConfigSubentry(
                data=MappingProxyType(
                    {CONF_STATE_ENTITY: state_entity, CONF_ENERGY_ENTITY: counter}
                ),
                subentry_type=SUBENTRY_APPLIANCE,
                title=device.name,
                unique_id=device.counter,
            )
        )
    return new, skipped
