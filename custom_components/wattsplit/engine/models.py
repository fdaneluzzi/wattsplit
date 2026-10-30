"""Plain data types shared by the WattSplit engine. No Home Assistant imports."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class PowerMode(StrEnum):
    """How much the engine may trust or change a device's declared power."""

    KNOWN = "known"  # declared levels are used as-is; nothing is learned
    PRIOR = "prior"  # declared level is a hint, refined from observations
    LEARN = "learn"  # nothing declared; learn it (state) or discover it (latent)
    PINNED = "pinned"  # a learned value the user froze; never touched again


@dataclass(frozen=True, slots=True)
class Edge:
    """A settled step in the aggregate power signal.

    ``ts`` is when the new level started; ``seen_ts`` is when the detector
    could first report it. Replay orders edges by ``seen_ts`` so it sees
    exactly what the live engine saw.
    """

    ts: float
    delta_w: float
    seen_ts: float | None = None


@dataclass(frozen=True, slots=True)
class DeviceSpec:
    """What the user (or HA) tells us about a device.

    The combination of flags decides how the engine treats it:

    - ``has_power``: a real power sensor exists. Its reading is subtracted from
      the aggregate and never estimated.
    - ``has_state``: an entity reports on/off. Power is *learned* from the
      aggregate steps that coincide with state changes.
    - neither: a latent device. On/off *and* power are inferred from steps in
      the aggregate that match ``power_levels_w``. With no levels declared the
      device waits for a discovered candidate to be assigned to it.

    ``power_levels_w`` holds nameplate or known power levels (e.g. a shower
    with winter/summer settings: ``(3200, 5500)``). ``power_mode`` defaults to
    what the declared levels imply; see :attr:`mode`.
    """

    device_id: str
    name: str
    power_levels_w: tuple[float, ...] = ()
    tolerance: float = 0.15
    has_state: bool = False
    has_power: bool = False
    power_mode: PowerMode | None = None

    @property
    def is_latent(self) -> bool:
        return not self.has_state and not self.has_power

    @property
    def mode(self) -> PowerMode:
        if self.power_mode is not None:
            return self.power_mode
        if not self.power_levels_w:
            return PowerMode.LEARN
        return PowerMode.PRIOR if self.has_state else PowerMode.KNOWN


@dataclass(frozen=True, slots=True)
class Estimate:
    """Current engine output for one device."""

    device_id: str
    is_on: bool
    power_w: float
    confidence: float  # 0..1
    # "declared" | "learned_from_state" | "inferred_from_edges" | "awaiting_discovery"
    source: str
