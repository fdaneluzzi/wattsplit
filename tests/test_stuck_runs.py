"""A run of an appliance with no entity must not stay "on" after a missed down-step.

Reported in issue #3 (a boiling-water tap) and seen on the author's house: a water
pump counted 56 hours of running in a week, 28 kWh in one day.
"""

import pytest
from engine import DeviceSpec, Disaggregator, Discoverer, Edge


def _pump() -> Disaggregator:
    return Disaggregator([DeviceSpec("pump", "Pump", power_levels_w=(1341.0,))])


def test_a_rise_the_size_of_the_device_while_it_is_on_restarts_its_run():
    diss = _pump()
    assert diss.on_edge(Edge(1000, 1330.0, 1000)) == "pump"
    assert diss.snapshot()["pump"].is_on
    # its fall was missed; the next run starts ten minutes later
    assert diss.on_edge(Edge(1600, 1350.0, 1600)) == "pump"
    assert diss.snapshot()["pump"].is_on
    assert diss.on_edge(Edge(1900, -1340.0, 1900)) == "pump"
    assert not diss.snapshot()["pump"].is_on
    # the old run was closed by the restart, so no run is left open
    assert diss._latent["pump"].forced_closes == 1


def test_a_rise_right_after_starting_is_not_taken_for_a_restart():
    diss = _pump()
    diss.on_edge(Edge(1000, 1330.0, 1000))
    # ten seconds later: some other load of about the same size, not the pump again
    assert diss.on_edge(Edge(1010, 1350.0, 1010)) is None


def test_a_closing_step_goes_to_the_open_run_over_a_weaker_state_claim():
    diss = Disaggregator(
        [
            DeviceSpec("pump", "Pump", power_levels_w=(1341.0,)),
            DeviceSpec("ac", "A/C", has_state=True, power_levels_w=(1100.0,)),
        ]
    )
    diss.on_edge(Edge(1000, 1340.0, 1000))
    diss.on_state("ac", False, 1500)
    # 6 % from the pump's power, 15 % from what the A/C is believed to draw
    assert diss.on_edge(Edge(1510, -1260.0, 1510)) == "pump"


def test_a_clearly_better_state_claim_still_wins():
    diss = Disaggregator(
        [
            DeviceSpec("pump", "Pump", power_levels_w=(1341.0,)),
            DeviceSpec("ac", "A/C", has_state=True, power_levels_w=(1230.0,)),
        ]
    )
    diss.on_edge(Edge(1000, 1340.0, 1000))
    diss.on_state("ac", False, 1500)
    assert diss.on_edge(Edge(1510, -1230.0, 1510)) == "ac"


def test_a_run_the_whole_home_cannot_contain_is_closed():
    diss = _pump()
    diss.on_edge(Edge(1000, 1340.0, 1000))
    # the house draws 400 W: the pump cannot be drawing 1341 W
    assert diss.check_coherence(1100, 400.0) == []  # starts the clock
    assert diss.snapshot()["pump"].is_on
    assert diss.check_coherence(1140, 400.0) == []  # 40 s: not yet
    assert diss.check_coherence(1150, 400.0) == ["pump"]
    assert not diss.snapshot()["pump"].is_on


def test_a_plausible_house_power_keeps_the_run_and_resets_the_clock():
    diss = _pump()
    diss.on_edge(Edge(1000, 1340.0, 1000))
    assert diss.check_coherence(1100, 400.0) == []
    assert diss.check_coherence(1130, 1900.0) == []  # the house is busy again
    assert diss.check_coherence(1170, 400.0) == []  # the clock restarted at 1170
    assert diss.check_coherence(1200, 400.0) == []
    assert diss.snapshot()["pump"].is_on


def test_only_as_many_runs_are_closed_as_the_house_requires():
    diss = Disaggregator(
        [
            DeviceSpec("shower", "Shower", power_levels_w=(7900.0,)),
            DeviceSpec("pump", "Pump", power_levels_w=(1341.0,)),
        ]
    )
    diss.on_edge(Edge(1000, 7900.0, 1000))
    diss.on_edge(Edge(1010, 1340.0, 1010))
    # 3 kW: the shower cannot be running, the pump could be
    diss.check_coherence(1100, 3000.0)
    assert diss.check_coherence(1150, 3000.0) == ["shower"]
    assert diss.snapshot()["pump"].is_on and not diss.snapshot()["shower"].is_on


def test_a_run_far_longer_than_any_it_finished_is_closed_even_if_the_house_is_busy():
    diss = _pump()
    t = 1000.0
    for _ in range(6):  # six normal 5-minute runs
        diss.on_edge(Edge(t, 1340.0, t))
        diss.on_edge(Edge(t + 300, -1340.0, t + 300))
        t += 3600
    limit = diss._latent["pump"].run_limit_s()
    assert limit == pytest.approx(1800.0)  # six times 5 minutes

    diss.on_edge(Edge(t, 1340.0, t))
    assert diss.check_coherence(t + limit - 10, 3000.0) == []
    assert diss.check_coherence(t + limit + 10, 3000.0) == ["pump"]


def test_there_is_no_run_limit_before_enough_runs_have_finished():
    diss = _pump()
    for k in range(3):
        diss.on_edge(Edge(1000 + k * 600, 1340.0, 0))
        diss.on_edge(Edge(1300 + k * 600, -1340.0, 0))
    assert diss._latent["pump"].run_limit_s() is None


def test_a_lost_fall_does_not_invent_a_run_of_hours():
    disc = Discoverer()
    disc.add(Edge(0.0, 900.0, 0.0))  # its fall was lost
    disc.add(Edge(10_000.0, 900.0, 10_000.0))
    disc.add(Edge(10_360.0, -900.0, 10_360.0))
    disc.add(Edge(20_000.0, 900.0, 20_000.0))
    disc.add(Edge(20_360.0, -900.0, 20_360.0))
    (cand,) = disc.candidates(min_count=2)
    assert cand.count == 2
    assert cand.median_duration_s == pytest.approx(360.0)


def test_a_stuck_run_that_ends_by_chance_does_not_widen_the_limit():
    diss = _pump()
    t = 1000.0
    for _ in range(6):
        diss.on_edge(Edge(t, 1340.0, t))
        diss.on_edge(Edge(t + 300, -1340.0, t + 300))
        t += 3600
    before = diss._latent["pump"].run_limit_s()
    diss.on_edge(Edge(t, 1340.0, t))
    diss.on_edge(Edge(t + 6 * 3600, -1340.0, t + 6 * 3600))  # six hours: not a real run
    assert diss._latent["pump"].run_limit_s() == before
