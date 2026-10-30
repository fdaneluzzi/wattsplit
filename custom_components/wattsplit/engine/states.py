"""Turning Home Assistant entities into "is this appliance on?".

Pure Python (no Home Assistant imports) so the rules are tested on their own.
An entity is described by its domain, its state string and its attributes.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

# States that mean "doing its job" or "not", for sensors that report a status
# as text (a washer's cycle, a dishwasher's program, ...). Anything not listed
# is unknown: it must not be read as a switch to off.
RUNNING_WORDS = frozenset(
    {
        "on", "running", "active", "washing", "rinsing", "spinning", "drying",
        "rinse_hold", "cool_down", "cooling", "heating", "refreshing", "detecting",
        "in_progress", "busy", "playing",
    }
)
IDLE_WORDS = frozenset(
    {
        "off", "power_off", "initial", "sleep", "reserved", "pause", "paused",
        "error", "end", "finished", "idle", "standby", "ready", "stopped", "done",
    }
)
_CLIMATE_ACTIVE = frozenset({"cooling", "heating", "drying", "fan"})
_UNKNOWN = frozenset({"unavailable", "unknown", "none", ""})


def on_off(domain: str, state: str, attributes: Mapping[str, Any] | None = None) -> bool | None:
    """True/False when the state tells, None when it cannot be told."""
    value = (state or "").lower()
    if value in _UNKNOWN:
        return None
    attributes = attributes or {}
    if domain == "climate":
        action = attributes.get("hvac_action")
        if action is not None:
            return str(action).lower() in _CLIMATE_ACTIVE
        return value != "off"
    if domain in ("sensor", "select", "input_select"):
        if value in RUNNING_WORDS:
            return True
        if value in IDLE_WORDS:
            return False
        return None
    return value == "on"


# --- choosing which entity of a device says "on/off" -----------------------

# Lower is better. A climate, fan or water heater says whether the appliance is
# *working*; a washer-style status sensor says which phase of a cycle it is in; a
# bare "power" switch only says it is plugged in and awake.
_DOMAIN_PRIORITY = {
    "climate": 0,
    "water_heater": 0,
    "humidifier": 0,
    "fan": 1,
    "vacuum": 1,
    "media_player": 2,
}
_AUX_WORDS = ("iluminacao", "iluminação", "light", "display", "beep", "economia", "eco_", "lock", "child")


def pick_state_entity(entities: Iterable[tuple[str, str, Mapping[str, Any]]]) -> str | None:
    """The best entity to read on/off from, among a device's entities.

    ``entities`` is ``(entity_id, state, attributes)`` for each entity of the
    device (the device class and options live in the attributes).
    """
    best: tuple[int, str] | None = None
    for entity_id, state, attributes in entities:
        domain = entity_id.split(".", 1)[0]
        rank: int | None = None
        if domain in _DOMAIN_PRIORITY:
            rank = _DOMAIN_PRIORITY[domain]
        elif domain == "sensor":
            options = {str(o).lower() for o in attributes.get("options") or ()}
            if options & RUNNING_WORDS and options & IDLE_WORDS:
                rank = 1  # a status enum that knows running from idle
        elif domain == "binary_sensor" and attributes.get("device_class") in ("running", "power"):
            rank = 3
        elif domain == "switch" and not any(w in entity_id for w in _AUX_WORDS):
            rank = 5
        if rank is not None and (best is None or (rank, entity_id) < best):
            best = (rank, entity_id)
    return best[1] if best else None


def pick_counter(siblings: Iterable[str], dashboard_counter: str) -> str:
    """The finest energy counter available for an appliance.

    The Energy dashboard usually points at a monthly counter, which a cloud
    integration may refresh only every few hours; a ``*_energy_today`` counter
    on the same device is typically refreshed every hour, which resolves
    appliance runs much better.
    """
    for entity_id in sorted(siblings):
        if entity_id.endswith("_energy_today") and entity_id.startswith("sensor."):
            return entity_id
    return dashboard_counter
