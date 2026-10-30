"""Diagnostics download: what the engine sees and believes.

Meant to be attached to a bug report. It contains sensor entity ids and learned
power values, but no credentials or location.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from .coordinator import WattSplitConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: WattSplitConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    return {
        "entry": {"data": dict(entry.data), "options": dict(entry.options)},
        "appliances": {
            sub.subentry_id: {"title": sub.title, "data": dict(sub.data)}
            for sub in entry.subentries.values()
        },
        **coordinator.diagnostics(),
        "last_update_success": coordinator.last_update_success,
    }
