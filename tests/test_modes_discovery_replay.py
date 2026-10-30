import pytest
from sim import DT, Load, aggregate, run

from engine import (
    DeviceSpec,
    Disaggregator,
    EdgeLog,
    PowerMode,
    StepDetector,
    replay,
)


def test_mode_defaults_follow_declared_levels():
    assert DeviceSpec("a", "a", has_state=True).mode is PowerMode.LEARN
    assert DeviceSpec("a", "a", (900,), has_state=True).mode is PowerMode.PRIOR
    assert DeviceSpec("a", "a", (5500,)).mode is PowerMode.KNOWN
    assert DeviceSpec("a", "a").mode is PowerMode.LEARN
    pinned = DeviceSpec("a", "a", (900,), has_state=True, power_mode=PowerMode.PINNED)
    assert pinned.mode is PowerMode.PINNED


def _ac_cycles(watts: float, n: int = 6):
    loads, events = [], []
    for k in range(n):
        start = 300 + k * 900
        loads.append(Load(start, start + 400, watts))
        events += [(start, "ac", True), (start + 400, "ac", False)]
    return loads, events


def test_known_state_device_uses_declared_power_and_never_learns():
    ac = DeviceSpec("ac", "A/C", (900,), has_state=True, power_mode=PowerMode.KNOWN)
    loads, events = _ac_cycles(1300)  # real draw differs from the declared 900 W
    hist = run(aggregate(6000, loads), Disaggregator([ac]), events)
    mid = dict(hist)[300.0 + 5 * 900 + 100]["ac"]

    assert mid.is_on and mid.power_w == 900
    assert mid.source == "declared"


def test_prior_mode_moves_from_hint_to_observed_power():
    ac = DeviceSpec("ac", "A/C", (900,), has_state=True)  # PRIOR
    loads, events = _ac_cycles(1100)  # within the 50% band, but not 900
    hist = run(aggregate(6000, loads), Disaggregator([ac]), events)
    mid = dict(hist)[300.0 + 5 * 900 + 100]["ac"]

    assert mid.power_w == pytest.approx(1100, rel=0.05)
    assert mid.source == "learned_from_state"


def test_pinned_value_survives_later_changes():
    ac = DeviceSpec("ac", "A/C", has_state=True)
    diss = Disaggregator([ac])
    loads, events = _ac_cycles(900, n=4)
    run(aggregate(4000, loads), diss, events)
    diss.pin("ac")

    # The device now draws 1500 W (e.g. a dirty filter). Pinned value must hold.
    diss.on_state("ac", True, 10_000.0)
    snap = diss.snapshot()["ac"]
    assert snap.source == "declared"
    assert snap.power_w == pytest.approx(900, rel=0.05)
    assert snap.confidence == 1.0


def _dishwasher_house():
    # Three recurring 4 400 W cycles of ~10 min, plus a one-off kettle.
    loads = [Load(s, s + 600, 4400) for s in (300, 1500, 2700)]
    loads.append(Load(2000, 2080, 1800))
    return aggregate(3600, loads)


def test_latent_device_without_power_waits_then_gets_assigned_a_candidate():
    dishwasher = DeviceSpec("dishwasher", "Dishwasher")  # no power declared
    diss = Disaggregator([dishwasher])
    hist = run(_dishwasher_house(), diss)

    waiting = hist[-1][1]["dishwasher"]
    assert waiting.source == "awaiting_discovery" and not waiting.is_on

    # The inbox shows the recurring load, not the one-off kettle.
    (cand,) = diss.candidates(min_count=2)
    assert cand.level_w == pytest.approx(4400, rel=0.05)
    assert cand.count == 3
    assert cand.median_duration_s == pytest.approx(600, abs=3 * DT)

    diss.assign_candidate("dishwasher", cand.level_w)
    assert diss.candidates(min_count=1)[0].level_w == pytest.approx(1800, rel=0.1)

    # From now on the same load is recognised as the dishwasher.
    later = [(t + 10_000, p) for t, p in aggregate(1400, [Load(300, 900, 4400)])]
    hist = dict(run(later, diss, detector=StepDetector(dt_s=DT)))
    assert hist[10_000.0 + 600]["dishwasher"].is_on
    assert hist[10_000.0 + 600]["dishwasher"].source == "inferred_from_edges"


def test_unmatched_state_step_is_not_lost_to_discovery():
    ac = DeviceSpec("ac", "A/C", (900,), has_state=True)
    diss = Disaggregator([ac])
    run(
        aggregate(900, [Load(300, 600, 6400)]),
        diss,
        [(300, "ac", True)],
    )
    (cand,) = diss.candidates(min_count=1)
    assert cand.level_w == pytest.approx(6400, rel=0.05)


def _logged_run(specs, samples, state_events, log):
    diss = Disaggregator(specs)
    det = StepDetector(dt_s=DT)
    events = sorted(state_events)
    for ts, power in samples:
        while events and events[0][0] <= ts:
            ev_ts, dev, on = events.pop(0)
            diss.on_state(dev, on, ev_ts)
            log.add_state(ev_ts, dev, on)
        if edge := det.update(ts, power):
            log.add_edge(edge, diss.on_edge(edge))
    return diss


def test_replay_from_sqlite_log_reproduces_live_result(tmp_path):
    specs = [
        DeviceSpec("shower", "Shower", (5500,)),
        DeviceSpec("ac", "A/C", has_state=True),
    ]
    ac_loads, ac_events = _ac_cycles(900, n=5)
    samples = aggregate(5200, ac_loads + [Load(1000, 1500, 5500)])

    path = tmp_path / "wattsplit.db"
    log = EdgeLog(path)
    live = _logged_run(specs, samples, ac_events, log)
    log.close()

    reopened = EdgeLog(path)  # survives a restart
    replayed = replay(reopened, specs)

    assert replayed.snapshot() == live.snapshot()
    assert replayed.snapshot()["ac"].power_w == 0  # off at the end
    assert reopened.edges(unexplained_only=True) == []


def test_replay_can_try_a_different_parameter():
    specs = [DeviceSpec("ac", "A/C", has_state=True)]
    log = EdgeLog()
    # State flips 30 s before the step shows up (slow meter).
    ac_loads = [Load(330, 730, 900)]
    _logged_run(specs, aggregate(1200, ac_loads), [(300, "ac", True)], log)

    strict = replay(log, specs, match_window_s=10.0)
    lenient = replay(log, specs, match_window_s=60.0)

    assert strict.snapshot()["ac"].confidence == 0.0
    assert lenient.snapshot()["ac"].confidence > 0.0


def test_log_prune_drops_old_rows():
    log = EdgeLog()
    from engine import Edge

    log.add_edge(Edge(10, 500, 12), None)
    log.add_edge(Edge(5000, 500, 5002), None)
    log.add_state(10, "ac", True)

    assert log.prune(before_ts=1000) == 2
    assert [e.ts for e, _ in log.edges()] == [5000]
    assert log.state_events() == []
