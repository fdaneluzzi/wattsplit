"""Build the whole-home consumption signal from the user's meters."""

from __future__ import annotations


def house_power_w(
    grid_w: float,
    solar_w: float = 0.0,
    battery_discharge_w: float = 0.0,
) -> float:
    """Consumption = grid import (negative when exporting) + local generation.

    ``grid_w`` is positive when importing. ``solar_w`` is production (>= 0).
    ``battery_discharge_w`` is positive when the battery feeds the house and
    negative when it charges.
    """
    return grid_w + solar_w + battery_discharge_w
