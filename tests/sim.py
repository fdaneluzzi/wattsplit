"""Tiny synthetic-house simulator used by the engine tests."""

from __future__ import annotations

import random
from dataclasses import dataclass

from engine import Disaggregator, StepDetector

DT = 2.0


@dataclass
class Load:
    start: float
    end: float
    watts: float


def aggregate(
    duration_s: float,
    loads: list[Load],
    base_w: float = 350.0,
    noise_w: float = 15.0,
    seed: int = 1,
) -> list[tuple[float, float]]:
    """Baseline + noise + a fridge-like cycling load + the given loads."""
    rng = random.Random(seed)
    out = []
    t = 0.0
    while t < duration_s:
        p = base_w + rng.gauss(0, noise_w)
        if (t % 1500) < 600:  # fridge compressor, below the detector threshold
            p += 120
        p += sum(load.watts for load in loads if load.start <= t < load.end)
        out.append((t, p))
        t += DT
    return out


def run(
    samples: list[tuple[float, float]],
    diss: Disaggregator,
    state_events: list[tuple[float, str, bool]] | None = None,
    detector: StepDetector | None = None,
) -> list[tuple[float, dict]]:
    """Feed samples and state events in time order; return snapshot per sample."""
    detector = detector or StepDetector(dt_s=DT)
    events = sorted(state_events or [])
    history = []
    for ts, power in samples:
        while events and events[0][0] <= ts:
            ev_ts, dev_id, is_on = events.pop(0)
            diss.on_state(dev_id, is_on, ev_ts)
        edge = detector.update(ts, power)
        if edge:
            diss.on_edge(edge)
        history.append((ts, diss.snapshot()))
    return history
