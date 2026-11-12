"""Settings > Repairs: ask the user what a recurring unexplained load is.

The engine notices loads that switch on again and again at the same power and
that no appliance explains (a shower, a pump, a compressor cycling). Instead of
hiding that in a sensor attribute, each biggest one becomes a Repairs item with a
short form, which is native to Home Assistant and needs no dashboard.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.repairs import RepairsFlow, RepairsFlowResult
from homeassistant.core import HomeAssistant
from homeassistant.helpers import selector

from .const import DOMAIN


class DiscoveredLoadFlow(RepairsFlow):
    """What is this recurring load?"""

    def __init__(self, data: dict[str, Any]) -> None:
        self._level_w = float(data.get("level_w") or 0)
        self._likely = data.get("likely_device") or None
        self._placeholders = {
            key: str(data.get(key, ""))
            for key in ("level_w", "count", "days", "hours", "minutes", "hour", "likely", "share", "recent")
        }
        self._placeholders["level"] = self._placeholders["level_w"]

    def _coordinator(self):
        entries = [
            e
            for e in self.hass.config_entries.async_entries(DOMAIN)
            if getattr(e, "runtime_data", None) is not None
        ]
        return entries[0].runtime_data if entries else None

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> RepairsFlowResult:
        if self._coordinator() is None:
            return self.async_abort(reason="not_loaded")
        # The first call carries the issue id, not an answer: only a real submission has `action`.
        if user_input is not None and "action" in user_input:
            return await getattr(self, f"async_step_{user_input['action']}")()
        options = (["part_of"] if self._likely else []) + ["assign", "new"]
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required("action", default=options[0]): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=options,
                            translation_key="repair_action",
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
            description_placeholders=self._placeholders,
        )

    async def async_step_part_of(self, user_input: dict[str, Any] | None = None) -> RepairsFlowResult:
        """It is its compressor / heater cycling: count it as that appliance's own."""
        self._coordinator().confirm_cycle_level(self._likely, self._level_w)
        return self.async_create_entry(data={})

    async def async_step_assign(self, user_input: dict[str, Any] | None = None) -> RepairsFlowResult:
        """It is an appliance the user already listed but gave no power."""
        coordinator = self._coordinator()
        options = coordinator.latent_options()
        if not options:
            return self.async_abort(reason="no_waiting_appliance")
        if user_input is not None and "appliance" in user_input:
            coordinator.assign_candidate(user_input["appliance"], self._level_w)
            return self.async_create_entry(data={})
        return self.async_show_form(
            step_id="assign",
            data_schema=vol.Schema(
                {
                    vol.Required("appliance"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=options, mode=selector.SelectSelectorMode.DROPDOWN
                        )
                    )
                }
            ),
            description_placeholders=self._placeholders,
        )

    async def async_step_new(self, user_input: dict[str, Any] | None = None) -> RepairsFlowResult:
        """It is something new: name it, and WattSplit starts following it."""
        name = (user_input or {}).get("name", "").strip()
        if name:
            self._coordinator().create_appliance(name, self._level_w)
            return self.async_create_entry(data={})
        return self.async_show_form(
            step_id="new",
            data_schema=vol.Schema({vol.Required("name"): selector.TextSelector()}),
            description_placeholders=self._placeholders,
        )


async def async_create_fix_flow(
    hass: HomeAssistant, issue_id: str, data: dict[str, str | int | float | None] | None
) -> RepairsFlow:
    """Create the flow for a discovered-load issue."""
    return DiscoveredLoadFlow(dict(data or {}))
