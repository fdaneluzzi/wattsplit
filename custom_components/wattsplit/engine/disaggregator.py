"""Attribute aggregate power steps to devices."""

from __future__ import annotations

from .devices import LatentDevice, StateDevice
from .discovery import Candidate, Discoverer
from .models import DeviceSpec, Edge, Estimate


class Disaggregator:
    """Routes each :class:`Edge` to the device that best explains it.

    Priority for one edge:

    1. A state device whose entity changed state just before the edge.
       Exactly one candidate is required; if two toggled together the step
       is ambiguous and is dropped (nothing is learned from it). A single
       candidate whose power is far off its prior is skipped, and the step
       falls through to the next rule.
    2. The latent device with the smallest relative error against its known
       power levels.
    3. Otherwise the edge is unexplained: it goes to the :class:`Discoverer`
       and stays in the "untracked" bucket downstream.
    """

    def __init__(
        self,
        specs: list[DeviceSpec],
        match_window_s: float = 15.0,
        discoverer: Discoverer | None = None,
    ) -> None:
        self._window = match_window_s
        self.discovery = discoverer or Discoverer()
        self._latent: dict[str, LatentDevice] = {}
        self._state: dict[str, StateDevice] = {}
        self._measured: set[str] = set()
        self._pending: dict[str, tuple[float, bool]] = {}
        for spec in specs:
            if spec.has_power:
                self._measured.add(spec.device_id)
            elif spec.has_state:
                self._state[spec.device_id] = StateDevice(spec)
            else:
                self._latent[spec.device_id] = LatentDevice(spec)

    def on_state(self, device_id: str, is_on: bool, ts: float) -> None:
        dev = self._state.get(device_id)
        if dev is None:
            return
        dev.is_on = is_on
        self._pending[device_id] = (ts, is_on)

    def on_edge(self, edge: Edge) -> str | None:
        """Return the device id the edge was attributed to, if any."""
        self._pending = {
            k: v for k, v in self._pending.items() if edge.ts - v[0] <= self._window
        }

        candidates = [
            dev_id
            for dev_id, (ts, turned_on) in self._pending.items()
            if edge.ts - ts >= -2.0 and (edge.delta_w > 0) == turned_on
        ]
        if len(candidates) > 1:
            return None
        if candidates:
            dev = self._state[candidates[0]]
            if dev.plausible(abs(edge.delta_w)):
                dev.observe_step(abs(edge.delta_w))
                del self._pending[candidates[0]]
                return dev.spec.device_id

        best: tuple[LatentDevice, float, float] | None = None
        for dev in self._latent.values():
            m = dev.match(edge.delta_w)
            if m and (best is None or m[1] < best[2]):
                best = (dev, m[0], m[1])
        if best:
            best[0].apply(best[1], edge.ts)
            return best[0].spec.device_id

        self.discovery.add(edge)
        return None

    def candidates(self, min_count: int = 2) -> list[Candidate]:
        return self.discovery.candidates(min_count)

    def assign_candidate(self, device_id: str, level_w: float) -> None:
        """Give a waiting latent device the power level the user picked."""
        self._latent[device_id].set_levels((level_w,))
        self.discovery.consume(level_w)

    def pin(self, device_id: str) -> None:
        """Freeze a state device's learned power."""
        self._state[device_id].pin()

    def snapshot(self) -> dict[str, Estimate]:
        out: dict[str, Estimate] = {}
        for lat in self._latent.values():
            source = "awaiting_discovery" if lat.awaiting_discovery else (
                "inferred_from_edges"
            )
            conf = 0.0 if lat.awaiting_discovery else 0.8
            out[lat.spec.device_id] = Estimate(
                lat.spec.device_id, lat.is_on, lat.level_w, conf, source
            )
        for dev in self._state.values():
            out[dev.spec.device_id] = Estimate(
                dev.spec.device_id,
                dev.is_on,
                dev.power_w,
                dev.confidence,
                "declared" if dev.frozen else "learned_from_state",
            )
        return out
