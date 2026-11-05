"""Config flow, options flow and the appliance subentry flow."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_BATTERY,
    CONF_ENERGY_ENTITY,
    CONF_EVIDENCE,
    CONF_EXPORT_ENERGY,
    CONF_GENERATION,
    CONF_GRID,
    CONF_HOUSE,
    CONF_IMPORT_ENERGY,
    CONF_INVERT_GRID,
    CONF_LOADS,
    CONF_MIN_STEP,
    CONF_POWER_ENTITY,
    CONF_POWER_LEVELS,
    CONF_POWER_MODE,
    CONF_SAMPLE_INTERVAL,
    CONF_STATE_ENTITY,
    DEFAULT_MIN_STEP_W,
    DEFAULT_SAMPLE_INTERVAL_S,
    DOMAIN,
    SUBENTRY_APPLIANCE,
)
from .energy_import import (
    appliances_from_dashboard,
    async_export_estimates_to_energy,
    async_read_energy_dashboard,
)
from .engine import PowerMode
from .helpers import format_levels, parse_levels, sources_from_entry

_POWER_SENSOR = selector.EntitySelector(
    selector.EntitySelectorConfig(domain="sensor", device_class="power")
)
_POWER_SENSORS = selector.EntitySelector(
    selector.EntitySelectorConfig(domain="sensor", device_class="power", multiple=True)
)
_SOURCE_LISTS = (CONF_GRID, CONF_LOADS, CONF_GENERATION, CONF_BATTERY)


def _source_fields() -> dict:
    """The measurement sources, by role (see ``engine/plan.py``)."""
    return {
        vol.Optional(CONF_HOUSE): _POWER_SENSOR,
        vol.Optional(CONF_GRID): _POWER_SENSORS,
        vol.Optional(CONF_LOADS): _POWER_SENSORS,
        vol.Optional(CONF_GENERATION): _POWER_SENSORS,
        vol.Optional(CONF_BATTERY): _POWER_SENSORS,
        vol.Optional(CONF_INVERT_GRID, default=False): bool,
    }


def _clean_sources(user_input: dict[str, Any]) -> dict[str, Any]:
    """Every source key explicit, so an emptied field really clears the sensor."""
    cleaned: dict[str, Any] = {k: list(user_input.get(k) or []) for k in _SOURCE_LISTS}
    cleaned[CONF_HOUSE] = user_input.get(CONF_HOUSE) or ""
    cleaned[CONF_INVERT_GRID] = bool(user_input.get(CONF_INVERT_GRID))
    return cleaned


def _has_consumption_source(sources: dict[str, Any]) -> bool:
    return bool(sources[CONF_HOUSE] or sources[CONF_GRID] or sources[CONF_LOADS])


class WattSplitConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            sources = _clean_sources(user_input)
            if _has_consumption_source(sources):
                return self.async_create_entry(title="WattSplit", data={}, options=sources)
            errors["base"] = "no_source"

        suggested = user_input
        if suggested is None and (imported := await async_read_energy_dashboard(self.hass)):
            suggested = {
                CONF_GRID: list(imported.grid),
                CONF_GENERATION: list(imported.generation),
                CONF_BATTERY: list(imported.battery),
            }
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(_source_fields()), suggested
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return WattSplitOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        return {SUBENTRY_APPLIANCE: ApplianceSubentryFlow}


class WattSplitOptionsFlow(OptionsFlow):
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            options = _clean_sources(user_input)
            imported = None
            if user_input.get(CONF_IMPORT_ENERGY):
                imported = await async_read_energy_dashboard(self.hass)
                for key, values in (
                    (CONF_GRID, imported.grid if imported else ()),
                    (CONF_GENERATION, imported.generation if imported else ()),
                    (CONF_BATTERY, imported.battery if imported else ()),
                ):
                    if not options[key] and values:
                        options[key] = list(values)
            if not _has_consumption_source(options):
                errors["base"] = "no_source"
            else:
                if imported:
                    new, _skipped = appliances_from_dashboard(
                        self.hass, self.config_entry, imported
                    )
                    for subentry in new:
                        self.hass.config_entries.async_add_subentry(
                            self.config_entry, subentry
                        )
                if user_input.get(CONF_EXPORT_ENERGY):
                    await async_export_estimates_to_energy(self.hass, self.config_entry)
                options[CONF_SAMPLE_INTERVAL] = user_input[CONF_SAMPLE_INTERVAL]
                options[CONF_MIN_STEP] = user_input[CONF_MIN_STEP]
                if user_input.get(CONF_EVIDENCE):  # empty = use the default
                    options[CONF_EVIDENCE] = list(user_input[CONF_EVIDENCE])
                return self.async_create_entry(data=options)

        schema = vol.Schema(
            {
                **_source_fields(),
                vol.Optional(CONF_EVIDENCE): _POWER_SENSORS,
                vol.Optional(CONF_IMPORT_ENERGY, default=False): bool,
                vol.Optional(CONF_EXPORT_ENERGY, default=False): bool,
                vol.Required(
                    CONF_SAMPLE_INTERVAL, default=DEFAULT_SAMPLE_INTERVAL_S
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1, max=10, step=1, unit_of_measurement="s"
                    )
                ),
                vol.Required(
                    CONF_MIN_STEP, default=DEFAULT_MIN_STEP_W
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=20, max=3000, step=10, unit_of_measurement="W"
                    )
                ),
            }
        )
        sources = sources_from_entry(self.config_entry)
        suggested = user_input or {
            CONF_HOUSE: sources.house,
            CONF_GRID: list(sources.grid),
            CONF_LOADS: list(sources.load),
            CONF_GENERATION: list(sources.generation),
            CONF_BATTERY: list(sources.battery),
            CONF_INVERT_GRID: sources.invert_grid,
            **self.config_entry.options,
        }
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
        )


class ApplianceSubentryFlow(ConfigSubentryFlow):
    """One appliance: optional state entity, optional power sensor, power mode."""

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        return await self._async_form("user", user_input, None)

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        subentry = self._get_reconfigure_subentry()
        suggested = {"name": subentry.title, **subentry.data}
        return await self._async_form("reconfigure", user_input, suggested)

    async def _async_form(
        self,
        step_id: str,
        user_input: dict[str, Any] | None,
        suggested: dict[str, Any] | None,
    ) -> SubentryFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            errors, data = self._validate(user_input)
            if not errors:
                name = user_input["name"].strip()
                if step_id == "reconfigure":
                    return self.async_update_and_abort(
                        self._get_entry(),
                        self._get_reconfigure_subentry(),
                        title=name,
                        data=data,
                    )
                return self.async_create_entry(title=name, data=data)
            suggested = user_input

        schema = vol.Schema(
            {
                vol.Required("name"): selector.TextSelector(),
                vol.Optional(CONF_STATE_ENTITY): selector.EntitySelector(),
                vol.Optional(CONF_POWER_ENTITY): _POWER_SENSOR,
                vol.Optional(CONF_ENERGY_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor", device_class="energy")
                ),
                vol.Optional(CONF_POWER_MODE): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[m.value for m in PowerMode],
                        translation_key=CONF_POWER_MODE,
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
                vol.Optional(CONF_POWER_LEVELS): selector.TextSelector(),
            }
        )
        return self.async_show_form(
            step_id=step_id,
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
        )

    @staticmethod
    def _validate(user_input: dict[str, Any]) -> tuple[dict[str, str], dict[str, Any]]:
        data = {
            k: v
            for k, v in user_input.items()
            if k != "name" and v not in (None, "")
        }
        errors: dict[str, str] = {}

        if data.get(CONF_POWER_ENTITY):
            # Measured: the sensor is the truth, nothing to declare or learn.
            data.pop(CONF_POWER_MODE, None)
            data.pop(CONF_POWER_LEVELS, None)
            return errors, data

        mode = data.get(CONF_POWER_MODE)
        levels = data.get(CONF_POWER_LEVELS)
        if levels:
            try:
                data[CONF_POWER_LEVELS] = format_levels(parse_levels(levels))
            except ValueError:
                errors[CONF_POWER_LEVELS] = "invalid_power"
                return errors, data

        if mode == PowerMode.LEARN:
            data.pop(CONF_POWER_LEVELS, None)  # nothing declared in learn mode
        elif mode in (PowerMode.KNOWN, PowerMode.PRIOR, PowerMode.PINNED):
            if not levels:
                errors[CONF_POWER_LEVELS] = "levels_required"
        elif not levels and not data.get(CONF_STATE_ENTITY):
            # No mode, no levels, no entity: a latent device that must be discovered.
            data[CONF_POWER_MODE] = PowerMode.LEARN.value
        return errors, data
