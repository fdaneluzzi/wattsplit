import pytest
from engine import DeviceSpec, Disaggregator, Discoverer, Edge
from sim import run_channels, three_channel_house


def _pairs(disc, level, starts, duration_s):
    for t in starts:
        disc.add(Edge(t, level, t))
        disc.add(Edge(t + duration_s, -level, t + duration_s))


def test_same_power_but_very_different_run_lengths_are_different_groups():
    """A pump that cycles for 6 minutes and a heater that runs for 2 hours can both
    draw about 1.34 kW; reporting them as one "load that runs 107 times" misleads."""
    disc = Discoverer()
    day = 86_400.0
    pump = [1_000_000 + d * day + k * 3_000 for d in range(6) for k in range(10)]
    heater = [1_000_000 + d * day + 40_000 for d in range(4)]
    _pairs(disc, 1340.0, pump, 360.0)
    _pairs(disc, 1345.0, heater, 7_200.0)

    cands = disc.candidates(min_count=3)
    assert len(cands) == 2
    pump_c = max(cands, key=lambda c: c.count)
    assert pump_c.count == 60 and pump_c.days == 6
    assert pump_c.median_duration_s == pytest.approx(360, rel=0.05)
    assert pump_c.coherent
    heater_c = min(cands, key=lambda c: c.count)
    assert heater_c.count == 4 and heater_c.median_duration_s == pytest.approx(7_200, rel=0.05)


def test_a_load_at_one_hour_of_the_day_is_concentrated_one_all_day_is_not():
    disc = Discoverer()
    day = 86_400.0
    _pairs(disc, 5500.0, [1_000_000 + d * day for d in range(5)], 600.0)
    (shower,) = disc.candidates(min_count=3)
    assert shower.concentrated and shower.days == 5

    disc = Discoverer()
    _pairs(disc, 800.0, [1_000_000 + k * 2 * 3_600.0 for k in range(12)], 300.0)
    (spread,) = disc.candidates(min_count=3)
    assert not spread.concentrated and len(spread.hours) >= 5


def test_runs_far_outside_the_typical_length_do_not_join_the_group():
    disc = Discoverer()
    t = 1_000_000.0
    for dur in (100, 120, 140, 150, 160, 180, 200, 90, 130, 170):
        _pairs(disc, 900.0, [t], float(dur))
        t += 10_000
    for dur in (3_000, 3_600):  # same power, but 20 times longer than the rest
        _pairs(disc, 900.0, [t], float(dur))
        t += 10_000
    main = max(disc.candidates(min_count=3), key=lambda c: c.count)
    assert main.count == 10 and main.coherent
    assert main.duration_p90_s < 300


def test_a_small_appliance_is_not_offered_as_owner_of_a_much_larger_load():
    samples, events = three_channel_house(3, cycles=40, n_other=12 * 8)
    diss = Disaggregator([DeviceSpec("ac", "A/C", has_state=True)])
    run_channels(samples, diss, events)
    learned = diss.state_device("ac").learned_w
    assert learned and learned < 600

    t0, day = 5_000_000.0, 86_400.0
    for d in range(5):
        start = t0 + d * day + d * 5 * 3600
        diss.on_state("ac", True, start)
        for k in range(6):
            rise = start + 600 + k * 900
            diss.on_edge(Edge(rise, 1340.0, rise))
            diss.on_edge(Edge(rise + 360, -1340.0, rise + 360))
        diss.on_state("ac", False, start + 7_000)
    big = [c for c in diss.candidates(min_count=3) if abs(c.level_w - 1340) < 100]
    assert big and all(c.likely_device is None for c in big)
