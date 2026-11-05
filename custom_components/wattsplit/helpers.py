"""Translate Home Assistant states and config into engine inputs."""

from __future__ import annotations

from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import State

from .const import (
    CONF_BATTERY,
    CONF_CHANNELS,
    CONF_CYCLE_LEVELS,
    CONF_GENERATION,
    CONF_GRID,
    CONF_HOUSE,
    CONF_INVERT_GRID,
    CONF_LOADS,
    CONF_POWER_ENTITY,
    CONF_POWER_LEVELS,
    CONF_POWER_MODE,
    CONF_SOLAR,
    CONF_STATE_ENTITY,
)
from .engine import DeviceSpec, PowerMode, Sources
from .engine.states import on_off

_TO_WATTS = {"W": 1.0, "kW": 1000.0, "mW": 0.001, "MW": 1_000_000.0}
_UNAVAILABLE = (STATE_UNAVAILABLE, STATE_UNKNOWN)


def power_w(state: State | None) -> float | None:
    """Read a power sensor in watts; None if unavailable or not a number."""
    if state is None or state.state in _UNAVAILABLE:
        return None
    factor = _TO_WATTS.get(state.attributes.get("unit_of_measurement", "W"))
    if factor is None:
        return None
    try:
        return float(state.state) * factor
    except ValueError:
        return None


def is_on(state: State | None) -> bool | None:
    """On/off of a state entity; None when it cannot be told."""
    if state is None:
        return None
    # Not state.domain: the recorder returns LazyState objects without it.
    domain = state.entity_id.split(".", 1)[0]
    return on_off(domain, state.state, state.attributes)


_TO_WH = {"Wh": 1.0, "kWh": 1000.0, "MWh": 1_000_000.0}


def energy_wh(state: State | None) -> float | None:
    """Read a cumulative energy sensor in Wh; None if unavailable or unknown unit."""
    if state is None or state.state in _UNAVAILABLE:
        return None
    factor = _TO_WH.get(state.attributes.get("unit_of_measurement", "Wh"))
    if factor is None:
        return None
    try:
        return float(state.state) * factor
    except ValueError:
        return None


def parse_levels(text: str) -> tuple[float, ...]:
    """``"5500"`` or ``"3200, 5500"`` -> sorted positive watt levels."""
    levels = sorted(float(part) for part in text.replace(";", ",").split(",") if part.strip())
    if not levels or any(level <= 0 for level in levels):
        raise ValueError(text)
    return tuple(levels)


def format_levels(levels: tuple[float, ...] | list[float]) -> str:
    return ", ".join(f"{level:g}" for level in levels)


def spec_from_subentry(subentry: ConfigSubentry) -> DeviceSpec:
    data = subentry.data
    levels = parse_levels(data[CONF_POWER_LEVELS]) if data.get(CONF_POWER_LEVELS) else ()
    mode = data.get(CONF_POWER_MODE)
    return DeviceSpec(
        device_id=subentry.subentry_id,
        name=subentry.title,
        power_levels_w=levels,
        has_state=bool(data.get(CONF_STATE_ENTITY)),
        has_power=bool(data.get(CONF_POWER_ENTITY)),
        power_mode=PowerMode(mode) if mode else None,
        cycle_levels_w=parse_levels(data[CONF_CYCLE_LEVELS]) if data.get(CONF_CYCLE_LEVELS) else (),
    )


def _as_tuple(value) -> tuple[str, ...]:
    """A sensor id, a list of them, or nothing -> a tuple without duplicates."""
    if not value:
        return ()
    items = [value] if isinstance(value, str) else list(value)
    return tuple(dict.fromkeys(i for i in items if i))


def sources_from_entry(entry) -> Sources:
    """Read the measurement sources from an entry.

    New entries keep everything in their options as lists. Entries made by
    earlier versions kept single sensors in the data (``grid_entity``,
    ``solar_entity``, ``battery_entity``, ``house_entity``) and the per-phase
    sensors in the ``channels`` option; those are still understood. A key
    present in the options is authoritative, so clearing a field in the options
    really removes the sensor.
    """
    data, options = entry.data, entry.options

    def pick(key: str, *legacy: str) -> tuple[str, ...]:
        if key in options:
            return _as_tuple(options[key])
        for k in (key, *legacy):
            if k in options:
                return _as_tuple(options[k])
            if k in data:
                return _as_tuple(data[k])
        return ()

    house = options[CONF_HOUSE] if CONF_HOUSE in options else data.get(CONF_HOUSE)
    return Sources(
        grid=pick(CONF_GRID),
        load=pick(CONF_LOADS, CONF_CHANNELS),
        generation=pick(CONF_GENERATION, CONF_SOLAR),
        battery=pick(CONF_BATTERY),
        house=house or None,
        invert_grid=bool(options.get(CONF_INVERT_GRID, data.get(CONF_INVERT_GRID, False))),
    )
