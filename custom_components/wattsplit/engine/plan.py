"""Which measurements the engine searches for steps in, and which it only weighs.

A home can have several solar systems, several inverters, several utility or
sub-meters, per-phase sensors, a battery... The engine does not care what the
hardware is. It cares about a sensor's *role*, which decides how its readings
are read and used:

``grid``
    Net power at the utility connection, import positive, export negative.
    The sign is flipped with ``invert_grid`` for meters that report it the
    other way. Searched for steps.
``load``
    Power consumed on a circuit, a phase, or the whole house, however it is
    supplied. Searched for steps.
``generation``
    Anything that produces power for the house (solar inverters, a generator),
    positive when producing. *Evidence only*: a cloud steps it like a load
    would, so it is never searched for loads.
``battery``
    Positive when discharging into the house, negative when charging.
    Evidence only.

``house`` is an optional ready-made whole-home consumption sensor.

Everything here is plain Python so it is tested without Home Assistant.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Sources:
    grid: tuple[str, ...] = ()
    load: tuple[str, ...] = ()
    generation: tuple[str, ...] = ()
    battery: tuple[str, ...] = ()
    house: str | None = None
    invert_grid: bool = False

    @property
    def usable(self) -> bool:
        """At least one source that says how much the house consumes."""
        return bool(self.house or self.load or self.grid)

    @property
    def everything(self) -> tuple[str, ...]:
        singles = (self.house,) if self.house else ()
        return (*singles, *self.grid, *self.load, *self.generation, *self.battery)


@dataclass(frozen=True, slots=True)
class Plan:
    kind: str  # "load" | "house" | "grid" | "composed"
    detect: tuple[str, ...]  # channel names searched for steps
    members: tuple[tuple[str, tuple[str, ...]], ...]  # channel -> sensors it is made of
    evidence: tuple[str, ...]  # sensors judged per appliance, never searched

    def sensors_of(self, channel: str) -> tuple[str, ...]:
        return dict(self.members)[channel]


Reader = Callable[[str], float | None]


def choose_plan(
    sources: Sources,
    period_of: Callable[[str], float | None],
    evidence: list[str] | None = None,
) -> Plan:
    """Decide what to search for steps in.

    Steps are best seen in the fastest, least delayed signal that contains
    them, and generation sensors are not part of it. Priority:

    1. ``load`` sensors, each its own channel (a quiet circuit shows small
       appliances that vanish in a total).
    2. a ready-made ``house`` total.
    3. the ``grid`` meter(s). When generation / battery sensors report about
       as fast as the slowest grid meter, they are added in so the channel is
       true consumption; otherwise (a cloud inverter at 60 s next to a 15 s
       meter) detection stays on the grid alone and generation becomes
       evidence.

    ``evidence`` overrides which sensors are used as evidence; by default only
    generation and battery sensors that are not already inside the detection
    signal.
    """
    extras = (*sources.generation, *sources.battery)

    if sources.load:
        kind, detect = "load", sources.load
        members = tuple((c, (c,)) for c in detect)
        default_evidence: tuple[str, ...] = ()
    elif sources.house:
        kind, detect = "house", ("house",)
        members = (("house", (sources.house,)),)
        default_evidence = ()
    else:
        grid_periods = [period_of(g) for g in sources.grid]
        slowest_grid = max((p for p in grid_periods if p), default=None)
        composed = bool(extras) and slowest_grid is not None
        if composed:
            for sensor in extras:
                p = period_of(sensor)
                composed = composed and p is not None and p <= 1.5 * slowest_grid
        if composed:
            kind, detect = "composed", ("composed",)
            members = (("composed", (*sources.grid, *extras)),)
            default_evidence = ()
        else:
            kind = "grid"
            detect = sources.grid if len(sources.grid) > 1 else ("grid",)
            members = (
                tuple((g, (g,)) for g in sources.grid)
                if len(sources.grid) > 1
                else (("grid", sources.grid),)
            )
            default_evidence = extras

    chosen = default_evidence if evidence is None else tuple(evidence)
    inside = {s for _, sensors in members for s in sensors}
    return Plan(kind, tuple(detect), members, tuple(e for e in chosen if e not in inside))


def consumption_w(sources: Sources, read: Reader) -> float | None:
    """Whole-house consumption, or None if a needed sensor is unavailable.

    A ready-made ``house`` sensor wins; else the ``load`` sensors are added up;
    else consumption is rebuilt as grid + generation + battery discharge.
    """
    if sources.house:
        return read(sources.house)
    if sources.load:
        values = [read(e) for e in sources.load]
        return None if any(v is None for v in values) else sum(values)
    parts: list[float] = []
    for sensor in sources.grid:
        value = read(sensor)
        if value is None:
            return None
        parts.append(-value if sources.invert_grid else value)
    for sensor in (*sources.generation, *sources.battery):
        value = read(sensor)
        if value is None:
            return None
        parts.append(value)
    return sum(parts)


def channel_values(plan: Plan, sources: Sources, read: Reader) -> dict[str, float]:
    """Readings for the plan's detection channels; unavailable ones are absent."""
    out: dict[str, float] = {}
    for channel, sensors in plan.members:
        values = [read(s) for s in sensors]
        if any(v is None for v in values):
            continue
        if plan.kind == "load":
            out[channel] = values[0]
        elif plan.kind == "house":
            out[channel] = values[0]
        else:  # grid (net, sign-corrected) or composed (+ generation, battery)
            signed = [
                -v if (s in sources.grid and sources.invert_grid) else v
                for s, v in zip(sensors, values)
            ]
            out[channel] = sum(signed)
    return out
