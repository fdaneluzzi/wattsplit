"""WattSplit: per-appliance power inferred from whole-house consumption.

M0 placeholder. The coordinator, config flow and entity platforms land in M1;
until then this only registers the domain so the engine can live beside it.
"""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return True
