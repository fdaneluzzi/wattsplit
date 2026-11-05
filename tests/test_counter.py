import pytest
from engine import CounterEstimator, DeviceSpec, Disaggregator, EdgeLog, replay
from sim import DT, run_channels, three_channel_house


def counter_series(windows, power_w, period_s, total_s, quant=1.0, reset_at=None):
    """A cumulative Wh counter, updated every ``period_s``, for an appliance on in ``windows``."""
    readings, cum, next_update = [(0.0, 0.0)], 0.0, period_s
    for t in range(1, int(total_s) + 1):
        if reset_at is not None and t == reset_at:
            cum = 0.0
        for a, b in windows:
            if a <= t < b:
                cum += (power_w(t) if callable(power_w) else power_w) / 3600.0
        if t >= next_update:
            readings.append((float(t), round(cum / quant) * quant))
            next_update += period_s
    return readings


def feed(est, windows, readings):
    est.add_state(0.0, False)
    events = sorted([(a, True) for a, _ in windows] + [(b, False) for _, b in windows])
    i = 0
    for ts, value in readings:
        while i < len(events) and events[i][0] <= ts:
            est.add_state(*events[i])
            i += 1
        est.add_counter(ts, value)


def test_hourly_counter_gives_the_power_while_on():
    windows = [(1000, 3000), (7200, 9000), (12000, 14500), (16000, 17000)]
    est = CounterEstimator()
    feed(est, windows, counter_series(windows, 800.0, 3600, 20000))
    power, confidence = est.estimate()

    assert power == pytest.approx(800, rel=0.03)
    assert confidence > 0.5


def test_a_counter_rollover_drops_that_interval_and_nothing_else():
    windows = [(1000, 3000), (7200, 9000), (12000, 14500), (16000, 17000)]
    readings = counter_series(windows, 800.0, 3600, 20000, reset_at=10000)
    est = CounterEstimator()
    feed(est, windows, readings)
    power, _ = est.estimate()
    assert power == pytest.approx(800, rel=0.04)


def test_a_variable_appliance_gets_its_energy_weighted_average():
    windows = [(1000, 3000), (7200, 9000), (12000, 14500), (16000, 17000)]
    # 300 W for the first part of each run, 500 W after: the average is in between
    power_fn = lambda t: 300.0 if (t % 100) < 50 else 500.0
    est = CounterEstimator()
    feed(est, windows, counter_series(windows, power_fn, 3600, 20000))
    power, _ = est.estimate()
    assert power == pytest.approx(400, rel=0.05)


def test_too_little_evidence_says_nothing():
    est = CounterEstimator()
    feed(est, [(1000, 1100)], counter_series([(1000, 1100)], 800.0, 3600, 8000))
    assert est.estimate() is None  # 100 s on: below the minimum on-time

    est = CounterEstimator()
    windows = [(1000, 3000)]
    feed(est, windows, counter_series(windows, 800.0, 3600, 8000))
    assert est.estimate() is None  # a single usable interval is not enough


def test_an_idle_counter_is_not_taken_for_a_running_device():
    windows = [(1000, 3000), (7200, 9000), (12000, 14500)]
    est = CounterEstimator()
    feed(est, windows, counter_series(windows, 0.0, 3600, 17000))  # counter never moves
    assert est.estimate() is None


def _spec():
    return DeviceSpec("ac", "A/C", has_state=True)


def _long_house(seed=3, cycles=40, period_s=1800):
    """Four hours of an A/C toggled every 3 minutes, in a busy three-channel house,
    plus the A/C's own cumulative energy counter, which reports every ``period_s``."""
    samples, events, truth = three_channel_house(
        seed, with_truth=True, cycles=cycles, n_other=12 * 8
    )
    cum, readings = 0.0, []
    for i, (ts, _) in enumerate(samples):
        cum += truth[i] * DT / 3600.0
        if ts % period_s == 0:
            readings.append((ts, float(round(cum))))
    return samples, events, readings


def test_the_counter_gives_an_estimate_with_no_house_channels_at_all():
    _, events, readings = _long_house()
    diss = Disaggregator([_spec()])
    diss.sync_state("ac", False, 0.0)
    for ts, _, on in events:
        diss.on_state("ac", on, ts)
    for ts, value in readings:
        diss.on_counter("ac", ts, value)
    est = diss.snapshot()["ac"]

    assert est.source == "learned_from_counter"
    assert est.channels == ()  # nothing from the house
    assert est.confidence >= 0.5
    # ramping inverter unit: its average over the on-time is about 340 W
    assert diss.state_device("ac").learned_w == pytest.approx(340, rel=0.2)


def test_counter_and_house_channels_cross_check_each_other():
    samples, events, readings = _long_house()
    diss = Disaggregator([_spec()])
    diss.sync_state("ac", False, 0.0)
    run_channels(samples, diss, events)  # learns from the house channels
    for ts, value in readings:  # then the appliance's own counter weighs in
        diss.on_counter("ac", ts, value)
    check = diss.cross_checks()["ac"]

    # two independent measurements of the same appliance agree (the channels see a
    # 7-60 s start-up lag and the window skip, the counter does not)
    assert abs(check["difference_pct"]) < 35, check


def test_counter_readings_survive_a_restart_through_the_log():
    _, events, readings = _long_house()
    log = EdgeLog()
    for ts, _, on in events:
        log.add_state(ts, "ac", on)
    for ts, value in readings:
        log.add_counter(ts, "ac", value)

    replayed = replay(log, [_spec()])
    est = replayed.state_device("ac").counter.estimate()
    assert est is not None and est[0] == pytest.approx(340, rel=0.2)


def test_a_variably_used_appliance_still_gets_a_confident_average_power():
    # Same 800 W while on, but each run uses it very differently (a thermostat that
    # cycles, a washer program that varies): per-interval power scatters by +-40 %.
    import random

    rng = random.Random(11)
    est = CounterEstimator()
    est.add_state(0.0, False)
    t, value = 0.0, 0.0
    for _ in range(40):
        run = rng.uniform(900, 3000)
        est.add_state(t + 600, True)
        est.add_state(t + 600 + run, False)
        value += 800 * rng.uniform(0.6, 1.4) * run / 3600.0 / 1.0
        t += 3600
        est.add_counter(t, float(round(value)))
    power, confidence = est.estimate()

    assert power == pytest.approx(800, rel=0.1)
    assert confidence > 0.5  # many intervals pin the ratio down, however uneven each was


def test_a_handful_of_wildly_uneven_intervals_is_not_trusted():
    import random

    rng = random.Random(5)
    est = CounterEstimator()
    est.add_state(0.0, False)
    t, value = 0.0, 0.0
    for _ in range(3):
        run = 1200.0
        est.add_state(t + 600, True)
        est.add_state(t + 600 + run, False)
        value += rng.choice([20, 900]) * run / 3600.0
        t += 3600
        est.add_counter(t, float(round(value)))
    result = est.estimate()
    assert result is None or result[1] < 0.3
