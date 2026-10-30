import random

import pytest
from sim import DT, aggregate, run, run_channels, three_channel_house

from engine import (
    DeviceSpec,
    Disaggregator,
    GlitchFilter,
    MultiChannelDetector,
    StepDetector,
)


def _feed(det, values):
    return [e for i, v in enumerate(values) if (e := det.update(i * DT, v))]


def test_threshold_adapts_to_channel_noise():
    rng = random.Random(1)
    quiet = StepDetector(dt_s=DT, min_step_w=60, noise_k=4)
    noisy = StepDetector(dt_s=DT, min_step_w=60, noise_k=4)
    _feed(quiet, [150 + rng.gauss(0, 3) for _ in range(300)])
    _feed(noisy, [1000 + rng.gauss(0, 250) for _ in range(300)])

    assert quiet.threshold_w == pytest.approx(60)  # the floor
    assert noisy.threshold_w > 800


def test_small_step_is_seen_on_a_quiet_channel_and_not_invented_on_a_noisy_one():
    rng = random.Random(2)

    def signal(sigma):
        return [(150 if i < 100 else 300) + rng.gauss(0, sigma) for i in range(200)]

    quiet = _feed(StepDetector(dt_s=DT, min_step_w=60, noise_k=4), signal(3))
    noisy = _feed(StepDetector(dt_s=DT, min_step_w=60, noise_k=4), signal(120))

    assert [round(e.delta_w, -1) for e in quiet] == [150]
    assert noisy == []


def test_a_load_spanning_two_channels_becomes_one_edge():
    det = MultiChannelDetector(["a", "b", "c"], dt_s=DT, min_step_w=300)
    rng = random.Random(3)
    edges = []
    for i in range(120):
        ts = i * DT
        readings = {
            "a": 500 + rng.gauss(0, 5) + (2750 if ts >= 100 else 0),
            "b": 400 + rng.gauss(0, 5) + (2750 if ts >= 104 else 0),  # lags 4 s
            "c": 900 + rng.gauss(0, 5),
        }
        if edge := det.update(ts, readings):
            edges.append(edge)

    (edge,) = edges
    assert edge.delta_w == pytest.approx(5500, rel=0.03)
    assert {c for c, _ in edge.parts} == {"a", "b"}


def test_a_single_channel_detector_adds_no_delay():
    plain, multi = StepDetector(dt_s=DT), MultiChannelDetector(["total"], dt_s=DT)
    values = [300] * 30 + [2300] * 30
    p = [i for i, v in enumerate(values) if plain.update(i * DT, v)]
    m = [i for i, v in enumerate(values) if multi.update(i * DT, {"total": v})]
    assert p == m and len(p) == 1


def test_glitch_filter_holds_back_a_brief_zero_but_accepts_a_real_one():
    f = GlitchFilter(max_hold_s=90)
    assert f.filter(0, 1000) == 1000
    assert [f.filter(t, 0.0) for t in (10, 40, 80)] == [None, None, None]
    assert f.filter(100, 1010) == 1010  # recovered: the zero never happened

    assert f.filter(110, 0.0) is None
    assert f.filter(205, 0.0) == 0.0  # still zero after 90 s: believe it
    assert f.filter(210, 0.0) == 0.0

    quiet = GlitchFilter()
    assert quiet.filter(0, 20) == 20
    assert quiet.filter(5, 0.0) == 0.0  # it was never a loaded signal


def test_rate_tracker_measures_the_real_reporting_period():
    from engine import RateTracker

    fast, slow, irregular = RateTracker(), RateTracker(), RateTracker()
    assert fast.period_s is None  # unknown until a few reports
    for i in range(10):
        fast.observe(i * 1.0)
        slow.observe(i * 61.0)
    rng = random.Random(5)
    t = 0.0
    for _ in range(40):
        t += rng.choice([14, 15, 15, 16, 60])  # mostly 15 s, an occasional gap
        irregular.observe(t)

    assert fast.period_s == pytest.approx(1)
    assert slow.period_s == pytest.approx(61)
    assert irregular.period_s == pytest.approx(15, abs=1)  # median ignores gaps


def test_timing_grows_with_the_slowest_input_and_keeps_defaults_when_unknown():
    from engine import timing_for

    unknown, meter, inverter = timing_for(None, 2), timing_for(15, 2), timing_for(61, 2)

    assert (unknown.settle_s, unknown.skip_s, unknown.window_s) == (6, 40, 120)
    assert meter.settle_s == pytest.approx(22.5) and meter.window_s == 120
    assert inverter.settle_s > meter.settle_s > unknown.settle_s
    assert inverter.window_s == pytest.approx(305) and inverter.min_window_s == pytest.approx(122)
    # What a 61 s sensor can resolve: runs of about 3.6 minutes and longer.
    assert inverter.shortest_run_s == pytest.approx(218.5, abs=1)
    assert meter.shortest_run_s < 100
    # A step must outlast at least one more report before it counts.
    assert meter.settle_s > 15 and inverter.settle_s > 61


def test_a_slow_sensor_cannot_resolve_short_cycles_and_says_so():
    # The same A/C data, but the only sensor reports every 61 s: windows grow to
    # ~8 min, which a 3-minute cycle cannot fill, so no toggle is trusted.
    from engine import timing_for

    samples, events = three_channel_house(3)
    tm = timing_for(61, DT)
    diss = Disaggregator([DeviceSpec("ac", "A/C", has_state=True)])
    diss.set_timing(tm.window_s, tm.window_s, tm.skip_s, tm.min_window_s)
    est = run_channels(samples, diss, events)[-1][1]["ac"]

    assert est.channels == ()  # no channel-mean evidence was accepted
    assert est.confidence < 0.3  # and what remains is weak, and says so


def test_quiet_channel_isolates_a_small_ac_among_noisy_channels():
    for seed in range(1, 13):
        samples, events = three_channel_house(seed)
        diss = Disaggregator([DeviceSpec("ac", "A/C", has_state=True)])
        est = run_channels(samples, diss, events)[-1][1]["ac"]

        # Ramping inverter unit: its average over the windows is 210-340 W.
        assert 150 <= diss._state["ac"].learned_w <= 400, seed
        assert [c for c, _ in est.channels] == ["b"], seed
        assert est.confidence > 0.5, seed


def test_without_a_usable_signal_it_says_it_does_not_know():
    # One channel that drifts by hundreds of watts; a 100 W appliance is lost
    # in it. The engine must stay quiet, not make up a number.
    rng = random.Random(4)
    drift, samples = 0.0, []
    for i in range(1800):
        drift = 0.98 * drift + rng.gauss(0, 40)
        on = any(s <= i * DT < s + 180 for s in (300, 660, 1020, 1380, 1740, 2100, 2460, 2820))
        samples.append((i * DT, 1000 + drift + (100 if on else 0)))
    events = [
        (t, "ac", on)
        for s in (300, 660, 1020, 1380, 1740, 2100, 2460, 2820)
        for t, on in ((s, True), (s + 180, False))
    ]
    diss = Disaggregator([DeviceSpec("ac", "A/C", has_state=True)])
    est = run(samples, diss, events)[-1][1]["ac"]

    assert est.confidence < 0.3


def _grid_plus_slow_inverter(cycle_s, seed=1, pump_w=1000.0, inverter_share=0.4):
    """A load fed partly by the grid, partly by an inverter that reports once a minute.

    ``inverter_share`` of the pump's power appears as extra inverter output (the
    inverter follows the load), the rest as extra grid import. Three on/off
    cycles of ``cycle_s`` seconds each.
    """
    rng = random.Random(seed)
    events, windows, t = [], [], 600.0
    for _ in range(3):
        windows.append((t, t + cycle_s))
        events += [(t, "pump", True), (t + cycle_s, "pump", False)]
        t += 2 * cycle_s
    drift, held, samples = 0.0, 4000.0, []
    ts = 0.0
    while ts < t + 300:
        on = any(a <= ts < b for a, b in windows)
        drift = 0.995 * drift + rng.gauss(0, 12)
        if ts % 60 == 0:  # the inverter reports once a minute and holds in between
            held = 4000 + drift + (pump_w * inverter_share if on else 0)
        grid = 500 + rng.gauss(0, 8) + (pump_w * (1 - inverter_share) if on else 0)
        samples.append((ts, {"grid": grid, "inverter": held}))
        ts += DT
    return samples, events


def _split_engine():
    from engine import timing_for

    diss = Disaggregator([DeviceSpec("pump", "Pump", has_state=True)])
    for channel, period in (("grid", 15), ("inverter", 61)):
        tm = timing_for(period, DT)
        diss.set_timing(tm.window_s, tm.window_s, tm.skip_s, tm.min_window_s, channel=channel)
    return diss


def test_a_load_fed_by_grid_and_inverter_counts_both_when_the_slow_channel_can_resolve_it():
    samples, events = _grid_plus_slow_inverter(cycle_s=1200)
    diss = _split_engine()
    est = run_channels(samples, diss, events, detect=["grid"])[-1][1]["pump"]

    parts = dict(est.channels)
    assert set(parts) == {"grid", "inverter"}
    assert parts["grid"] == pytest.approx(600, rel=0.1)
    assert parts["inverter"] == pytest.approx(400, rel=0.25)
    assert diss._state["pump"].learned_w == pytest.approx(1000, rel=0.15)


def test_when_the_slow_channel_cannot_resolve_the_cycle_the_estimate_is_partial_and_says_so():
    # 3-minute cycles are shorter than the inverter's averaging windows, so only
    # the grid share is measured. This is a limit of the data, not hidden.
    samples, events = _grid_plus_slow_inverter(cycle_s=180)
    diss = _split_engine()
    est = run_channels(samples, diss, events, detect=["grid"])[-1][1]["pump"]

    assert [c for c, _ in est.channels] == ["grid"]
    assert diss._state["pump"].learned_w == pytest.approx(600, rel=0.1)


def test_estimate_only_channels_never_produce_edges():
    # A cloud passing over the solar array steps the inverter; that is not a load.
    samples = [(i * DT, {"grid": 500.0, "inverter": 4000.0 if i < 200 else 2500.0}) for i in range(400)]
    diss = Disaggregator([DeviceSpec("pump", "Pump", has_state=True)])
    run_channels(samples, diss, detect=["grid"])
    assert diss.candidates(min_count=1) == []


def test_an_appliance_that_runs_entirely_on_the_slow_inverter_is_still_learned():
    # 100 % of the pump's power shows up on the 61 s inverter; the grid sees nothing.
    samples, events = _grid_plus_slow_inverter(cycle_s=600, inverter_share=1.0)
    diss = _split_engine()
    est = run_channels(samples, diss, events, detect=["grid"])[-1][1]["pump"]

    assert [c for c, _ in est.channels] == ["inverter"]
    assert diss._state["pump"].learned_w == pytest.approx(1000, rel=0.15)


def test_the_shortest_run_a_61s_channel_can_resolve_is_about_three_and_a_half_minutes():
    from engine import timing_for

    shortest = timing_for(61, DT).shortest_run_s
    assert 200 < shortest < 240

    # 5-minute runs are above it: measurable (noisier); 3-minute runs are not.
    samples, events = _grid_plus_slow_inverter(cycle_s=300, inverter_share=1.0, seed=2)
    diss = _split_engine()
    est = run_channels(samples, diss, events, detect=["grid"])[-1][1]["pump"]
    assert [c for c, _ in est.channels] == ["inverter"]

    samples, events = _grid_plus_slow_inverter(cycle_s=180, inverter_share=1.0, seed=2)
    diss = _split_engine()
    est = run_channels(samples, diss, events, detect=["grid"])[-1][1]["pump"]
    assert est.channels == ()  # it says it cannot tell, rather than guessing


def test_with_two_solar_systems_only_the_one_that_follows_the_appliance_counts():
    rng = random.Random(3)
    events, windows, t = [], [], 600.0
    for _ in range(3):
        windows.append((t, t + 900))
        events += [(t, "pump", True), (t + 900, "pump", False)]
        t += 1800
    d1 = d2 = 0.0
    h1, h2, samples = 5000.0, 3000.0, []
    ts = 0.0
    while ts < t + 300:
        on = any(a <= ts < b for a, b in windows)
        d1 = 0.99 * d1 + rng.gauss(0, 30)  # a cloudy array that wanders
        d2 = 0.995 * d2 + rng.gauss(0, 8)
        if ts % 60 == 0:
            h1 = 5000 + d1
        if (ts + 20) % 60 == 0:  # the second inverter reports out of step
            h2 = 3000 + d2 + (800 if on else 0)
        samples.append((ts, {"grid": 400 + rng.gauss(0, 8), "inv1": h1, "inv2": h2}))
        ts += DT

    from engine import timing_for

    diss = Disaggregator([DeviceSpec("pump", "Pump", has_state=True)])
    tm = timing_for(60, DT)
    for channel in ("inv1", "inv2"):
        diss.set_timing(tm.window_s, tm.window_s, tm.skip_s, tm.min_window_s, channel=channel)
    tg = timing_for(15, DT)
    diss.set_timing(tg.window_s, tg.window_s, tg.skip_s, tg.min_window_s, channel="grid")
    est = run_channels(samples, diss, events, detect=["grid"])[-1][1]["pump"]

    assert [c for c, _ in est.channels] == ["inv2"]
    assert diss._state["pump"].learned_w == pytest.approx(800, rel=0.15)


def test_a_declared_shower_fed_entirely_by_the_slow_inverter_is_detected_without_polluting_discovery():
    from engine import timing_for

    rng = random.Random(7)
    tm = timing_for(61, DT)
    shower_on, shower_off = 6000.0, 6900.0
    held, drift, samples = 4000.0, 0.0, []
    ts = 0.0
    while ts < 9000:
        drift = 0.995 * drift + rng.gauss(0, 15)
        cloud = -2500.0 if 1500 <= ts < 2700 else 0.0  # a cloud, long before the shower
        if ts % 61 < DT:
            held = 4000 + drift + cloud + (5500 if shower_on <= ts < shower_off else 0)
        samples.append((ts, {"grid": 300 + rng.gauss(0, 5), "inverter": held}))
        ts += DT

    shower = DeviceSpec("shower", "Shower", power_levels_w=(5500,))
    diss = Disaggregator([shower])
    evidence = MultiChannelDetector(
        ["inverter"],
        dt_s=DT,
        min_step_w=100.0,
        noise_k=4.0,
        settle_s=tm.settle_s,
        ref_s=tm.ref_s,
    )
    hist = dict(
        run_channels(
            samples, diss, detect=[], evidence_detector=evidence
        )
    )

    # The cloud's dip and recovery are not a shower, and not a "discovered" load either.
    assert not hist[2000.0]["shower"].is_on
    assert not hist[3200.0]["shower"].is_on
    assert diss.candidates(min_count=1) == []
    # The shower is seen, late by the sensor's own reporting delay, and leaves.
    assert hist[shower_on + 300]["shower"].is_on
    assert hist[shower_off + 300]["shower"].power_w == 0


def test_a_known_footprint_takes_its_share_of_a_merged_edge_and_frees_the_rest():
    from engine import Edge

    samples, events = three_channel_house(5)
    diss = Disaggregator([DeviceSpec("ac", "A/C", has_state=True)])
    run_channels(samples, diss, events)
    footprint = diss._state["ac"].footprint
    assert list(footprint) == ["b"]  # the A/C lives on the quiet channel

    # A 220 V pump (channels a + c) switches on, and a minute later it switches
    # off in the *same meter report* as the A/C: one merged edge.
    pump = {"a": 1149.0, "c": 1130.0}
    diss.on_edge(Edge(10100.0, sum(pump.values()), 10110.0, tuple(pump.items())))
    diss.on_state("ac", False, 10200.0)
    b = footprint["b"]
    merged = Edge(
        10205.0,
        -(sum(pump.values()) + b),
        10220.0,
        (("a", -1149.0), ("c", -1130.0), ("b", -b)),
    )
    assert diss.on_edge(merged) is None  # the pump's share is still nobody's

    # With the A/C's share taken out, the pump's off step matches its on step.
    pumps = [c for c in diss.candidates(min_count=1) if abs(c.level_w - 2279) < 60]
    assert len(pumps) == 1 and pumps[0].level_w == pytest.approx(2279, rel=0.02)


def test_without_a_footprint_a_merged_edge_is_not_split():
    from engine import Edge

    diss = Disaggregator([DeviceSpec("ac", "A/C", has_state=True)])
    diss.on_edge(Edge(100.0, 2279.0, 110.0, (("a", 1149.0), ("c", 1130.0))))
    diss.on_state("ac", False, 200.0)
    diss.on_edge(Edge(205.0, -2616.0, 220.0, (("a", -1149.0), ("c", -1130.0), ("b", -337.0))))
    assert diss.candidates(min_count=1) == []  # 2279 vs 2616: not the same load


def test_validator_scores_the_engine_against_a_real_power_sensor():
    from engine import Validator, timing_for

    errors = []
    for seed in range(1, 9):
        samples, events, truth = three_channel_house(seed, with_truth=True)
        validator = Validator("ac", "A/C")
        tm = timing_for(15, DT)
        for channel in samples[0][1]:
            validator.set_timing(tm.window_s, tm.window_s, tm.skip_s, tm.min_window_s, channel=channel)
        pending = sorted(events)
        for (ts, readings), true_w in zip(samples, truth):
            while pending and pending[0][0] <= ts:
                validator.on_state(pending[0][2], pending[0][0])
                pending.pop(0)
            validator.on_sample(ts, readings, true_w)
        report = validator.report()
        assert report["channels"].keys() == {"b"}, (seed, report)
        errors.append(abs(report["error_pct"]))

    assert max(errors) < 20  # shadow estimate within 20 % of the real sensor
    assert sum(errors) / len(errors) < 10
