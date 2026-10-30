"""Tiny synthetic-house simulators used by the engine tests."""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass

from engine import Disaggregator, MultiChannelDetector, StepDetector, Toggle

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
    on_toggle: Callable[[Toggle], None] | None = None,
) -> list[tuple[float, dict]]:
    """Feed samples and state events in time order; return snapshot per sample.

    Single-channel house: the signal is one channel called ``total``.
    """
    detector = detector or StepDetector(dt_s=DT)
    events = sorted(state_events or [])
    history = []
    for ts, power in samples:
        while events and events[0][0] <= ts:
            ev_ts, dev_id, is_on = events.pop(0)
            diss.on_state(dev_id, is_on, ev_ts)
        for toggle in diss.on_sample(ts, {"total": power}):
            if on_toggle:
                on_toggle(toggle)
        edge = detector.update(ts, power)
        if edge:
            diss.on_edge(edge)
        history.append((ts, diss.snapshot()))
    return history


def run_channels(
    samples: list[tuple[float, dict[str, float]]],
    diss: Disaggregator,
    state_events: list[tuple[float, str, bool]] | None = None,
    detector: MultiChannelDetector | None = None,
    on_toggle: Callable[[Toggle], None] | None = None,
    detect: list[str] | None = None,
    evidence_detector: MultiChannelDetector | None = None,
) -> list[tuple[float, dict]]:
    """Same as :func:`run` for a house measured on several channels.

    ``detect`` limits which channels are searched for steps; the others are
    estimate-only evidence (e.g. a solar inverter).
    """
    channels = list(samples[0][1]) if detect is None else detect
    detector = detector or MultiChannelDetector(
        channels, dt_s=DT, min_step_w=100.0, noise_k=4.0
    )
    events = sorted(state_events or [])
    history = []
    for ts, readings in samples:
        while events and events[0][0] <= ts:
            ev_ts, dev_id, is_on = events.pop(0)
            diss.on_state(dev_id, is_on, ev_ts)
        for toggle in diss.on_sample(ts, readings):
            if on_toggle:
                on_toggle(toggle)
        edge = detector.update(ts, {c: readings[c] for c in channels})
        if edge:
            diss.on_edge(edge)
        if evidence_detector is not None:
            names = evidence_detector.channels
            if edge := evidence_detector.update(ts, {c: readings[c] for c in names}):
                diss.on_edge(edge, discover=False)
        history.append((ts, diss.snapshot()))
    return history


def _ac_loads(
    rng: random.Random,
    ac_w: float,
    t0: float = 300.0,
    cycles: int = 5,
    ramp: bool = False,
):
    """A small A/C toggled ``cycles`` times, 3 min on / 3 min off.

    Its draw appears 7-60 s after each state change (meter and compressor
    lag). With ``ramp`` it is an inverter unit: half power for the first
    minute, then full power.
    """
    loads, events, t = [], [], t0
    for _ in range(cycles):
        on, off = t, t + 180
        start, end = on + rng.uniform(7, 60), off + rng.uniform(7, 60)
        if ramp:
            loads.append(Load(start, end, ac_w * 0.5))
            loads.append(Load(start + 60, end, ac_w * 0.5))
        else:
            loads.append(Load(start, end, ac_w))
        events += [(on, "ac", True), (off, "ac", False)]
        t += 360
    return loads, events, t


def busy_ac_house(seed: int, n_other: int = 25, ac_w: float = 340.0):
    """One measured channel: an A/C in a house full of unrelated loads.

    About one unrelated step per minute, 300-1800 W each, like a real house.
    """
    rng = random.Random(seed)
    loads, events, t = _ac_loads(rng, ac_w)
    for _ in range(n_other):
        start = rng.uniform(0, t)
        loads.append(Load(start, start + rng.uniform(60, 200), rng.uniform(300, 1800)))
    return aggregate(t + 200, loads, seed=seed), events


def three_channel_house(
    seed: int,
    ac_w: float = 340.0,
    n_other: int = 12,
    with_truth: bool = False,
    cycles: int = 5,
):
    """The same busy house measured on three channels (e.g. three phases).

    The A/C sits on the quiet channel ``b``; ``a`` and ``c`` carry the
    unrelated loads and a lot of noise. Returns samples as
    ``(ts, {channel: watts})`` plus the A/C's state events.
    """
    rng = random.Random(seed)
    ac_loads, events, t = _ac_loads(rng, ac_w, ramp=True, cycles=cycles)
    others = {"a": [], "c": []}
    for name in others:
        for _ in range(n_other):
            start = rng.uniform(0, t)
            others[name].append(
                Load(start, start + rng.uniform(60, 200), rng.uniform(300, 1800))
            )
    cfg = {"a": (1400.0, 40.0, others["a"]), "b": (150.0, 4.0, ac_loads), "c": (1200.0, 40.0, others["c"])}
    noise = random.Random(seed + 1000)
    samples, truth = [], []
    ts = 0.0
    while ts < t + 200:
        truth.append(sum(l.watts for l in ac_loads if l.start <= ts < l.end))
        samples.append(
            (
                ts,
                {
                    ch: base + noise.gauss(0, sigma) + sum(l.watts for l in loads if l.start <= ts < l.end)
                    for ch, (base, sigma, loads) in cfg.items()
                },
            )
        )
        ts += DT
    return (samples, events, truth) if with_truth else (samples, events)
