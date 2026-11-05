import pytest
from engine import (
    DeviceSpec,
    Disaggregator,
    Discoverer,
    Edge,
    StateDebouncer,
    Toggle,
)
from sim import run_channels, three_channel_house


def test_evidence_per_channel_is_no_longer_capped_at_ten_events():
    samples, events = three_channel_house(3, cycles=40, n_other=12 * 8)
    diss = Disaggregator([DeviceSpec("ac", "A/C", has_state=True)])
    run_channels(samples, diss, events)
    stats = {c["channel"]: c["n"] for c in diss.state_device("ac").describe()["channels"]}

    # forty cycles are eighty state changes: every channel keeps far more than the old 10
    assert all(n >= 60 for n in stats.values()), stats


def test_discovery_remembers_far_more_than_the_old_500_edges():
    disc = Discoverer()
    t = 0.0
    for _ in range(1500):  # 3000 edges: 1500 pairs of the same 700 W load
        disc.add(Edge(t, 700.0, t))
        disc.add(Edge(t + 300, -700.0, t + 300))
        t += 600
    (cand,) = disc.candidates(min_count=2)
    assert cand.count == 1500 and cand.level_w == pytest.approx(700, rel=0.02)


def _ac_with_cycling_compressor():
    """An A/C that runs 1.5 h a day at a different hour each day; its compressor cycles
    1300 W for 6 min, only while it runs. (At the same hour every day, time of day
    alone would explain the coincidence, so the test would prove nothing.)"""
    diss = Disaggregator([DeviceSpec("ac", "A/C", has_state=True), DeviceSpec("tv", "TV", has_state=True)])
    t0, day = 1_000_000.0, 86_400.0
    for d in range(5):
        start = t0 + d * day + d * 7 * 3600
        diss.on_state("ac", True, start)
        for k in range(6):  # six compressor cycles per run
            rise = start + 600 + k * 800
            diss.on_edge(Edge(rise, 1300.0, rise))
            diss.on_edge(Edge(rise + 360, -1300.0, rise + 360))
        diss.on_state("ac", False, start + 5_400)
        diss.on_state("tv", True, start + 30_000)  # something else is on for a while
        diss.on_state("tv", False, start + 36_000)
    return diss, t0 + 8 * day


def test_a_load_that_only_appears_while_a_device_runs_is_handed_to_that_device():
    diss, end = _ac_with_cycling_compressor()
    cands = diss.candidates(min_count=2)
    # clearly the A/C's compressor (on in all occurrences, ~15 % of those hours): adopted,
    # so it leaves the inbox instead of being offered as a "new appliance"
    assert not any(abs(c.level_w - 1300) < 100 for c in cands)
    assert any(abs(lv - 1300) < 100 for lv in diss.state_device("ac").cycle_levels)

    # and from now on its cycling is its own, but only while it runs
    diss.on_state("ac", True, end + 100)
    assert diss.on_edge(Edge(end + 500, 1310.0, end + 500)) == "ac"
    diss.on_state("ac", False, end + 900)
    assert diss.on_edge(Edge(end + 1500, 1310.0, end + 1500)) is None


def test_a_less_certain_association_is_suggested_not_applied():
    diss = Disaggregator([DeviceSpec("ac", "A/C", has_state=True)])
    t0, day = 1_000_000.0, 86_400.0
    diss.on_state("ac", True, t0)
    diss.on_state("ac", False, t0 + 8 * 3600)
    # eight occurrences (under the adoption threshold), all while the A/C was on
    for k in range(8):
        rise = t0 + 600 + k * 3000
        diss.on_edge(Edge(rise, 900.0, rise))
        diss.on_edge(Edge(rise + 300, -900.0, rise + 300))
    diss.on_edge(Edge(t0 + 3 * day, 5.0, t0 + 3 * day))  # the A/C stayed off at those hours on the other days
    (cand,) = diss.candidates(min_count=2)

    assert cand.likely_device == "ac" and cand.lift >= 3 and cand.share_on == pytest.approx(1.0)
    assert not diss.state_device("ac").cycle_levels  # nothing applied without confirmation


def test_a_confirmed_cycle_level_can_be_added_by_the_user():
    diss = Disaggregator([DeviceSpec("ac", "A/C", has_state=True)])
    diss.add_cycle_level("ac", 900.0)
    diss.on_state("ac", True, 100.0)
    assert diss.on_edge(Edge(200.0, 880.0, 200.0)) == "ac"


def test_a_flap_is_not_a_state_change_but_a_held_change_is_with_its_own_timestamp():
    deb = StateDebouncer(delay_s=20)
    deb.sync("ac", False)

    deb.submit("ac", 100.0, True)  # off -> on
    deb.submit("ac", 106.0, False)  # ...and back off 6 s later: it never happened
    assert deb.poll(200.0) == []

    deb.submit("ac", 300.0, True)  # a real one
    assert deb.poll(310.0) == []  # not yet held for 20 s
    assert deb.poll(321.0) == [("ac", 300.0, True)]  # confirmed, original time kept
    assert deb.poll(400.0) == []  # reported once

    deb.submit("ac", 500.0, False)
    deb.submit("ac", 502.0, True)  # off then on again quickly while it was on: nothing
    assert deb.poll(600.0) == []


def test_the_first_reading_of_an_entity_is_a_baseline_not_a_change():
    deb = StateDebouncer(delay_s=20)
    deb.submit("pump", 10.0, True)
    assert deb.poll(100.0) == []
    deb.submit("pump", 200.0, False)
    assert deb.poll(230.0) == [("pump", 200.0, False)]


def test_a_looser_channel_estimate_is_compared_to_the_counter_and_marked_weak():
    diss = Disaggregator([DeviceSpec("ac", "A/C", has_state=True)])
    # six ON toggles on one channel: leaning clearly the same way, but too scattered
    # to pass the strict test (z about 5)
    for i, diff in enumerate([100, 400, 250, 320, 180, 330]):
        diss.apply_toggle(
            Toggle("ac", 1000.0 * i, True, (("b", 150.0),), (("b", 150.0 + diff),), 1000.0 * i + 200)
        )
    dev = diss.state_device("ac")
    assert dev._channel_estimate() is None  # not strict
    assert dev.provisional_w == pytest.approx(285, rel=0.05)

    # its own energy counter: 2400 s on per hour at 285 W, twice
    dev.counter.add_state(0.0, False)
    dev.counter.add_state(600.0, True)
    dev.counter.add_state(3000.0, False)
    dev.counter.add_state(4200.0, True)
    dev.counter.add_state(6600.0, False)
    for ts, wh in ((0.0, 0.0), (3600.0, 190.0), (7200.0, 380.0)):
        dev.counter.add_counter(ts, wh)

    check = diss.cross_checks()["ac"]
    assert check["strength"] == "weak"
    assert abs(check["difference_pct"]) < 10
