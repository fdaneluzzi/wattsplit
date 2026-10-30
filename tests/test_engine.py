import pytest
from sim import DT, Load, aggregate, run

from engine import DeviceSpec, Disaggregator, StepDetector, house_power_w


def test_house_power_adds_generation():
    assert house_power_w(grid_w=-800, solar_w=2300) == 1500
    assert house_power_w(grid_w=500, solar_w=0, battery_discharge_w=-200) == 300


def test_detector_reports_one_clean_edge_per_step():
    samples = aggregate(300, [Load(100, 200, 2000)], noise_w=5)
    det = StepDetector(dt_s=DT)
    edges = [e for ts, p in samples if (e := det.update(ts, p))]
    assert [round(e.delta_w, -2) for e in edges] == [2000, -2000]
    assert edges[0].ts == pytest.approx(100, abs=DT)
    assert edges[1].ts == pytest.approx(200, abs=DT)


def test_detector_ignores_steps_below_threshold():
    samples = aggregate(300, [Load(100, 200, 120)], noise_w=5)
    det = StepDetector(dt_s=DT, min_step_w=300)
    assert not [e for ts, p in samples if det.update(ts, p)]


def test_latent_shower_is_detected_without_any_entity():
    shower = DeviceSpec("shower", "Shower", power_levels_w=(5500,))
    samples = aggregate(2400, [Load(600, 1100, 5500)])
    hist = dict(run(samples, Disaggregator([shower])))

    assert not hist[500.0]["shower"].is_on
    assert hist[900.0]["shower"].is_on
    assert hist[900.0]["shower"].power_w == 5500
    assert not hist[1300.0]["shower"].is_on


def test_two_latent_devices_are_told_apart_by_power():
    specs = [
        DeviceSpec("shower", "Shower", power_levels_w=(5500,)),
        DeviceSpec("kettle", "Kettle", power_levels_w=(1800,)),
    ]
    loads = [Load(300, 900, 5500), Load(500, 560, 1800)]
    hist = dict(run(aggregate(1500, loads), Disaggregator(specs)))

    assert hist[400.0]["shower"].is_on and not hist[400.0]["kettle"].is_on
    assert hist[530.0]["shower"].is_on and hist[530.0]["kettle"].is_on
    assert hist[700.0]["shower"].is_on and not hist[700.0]["kettle"].is_on
    assert not hist[1000.0]["shower"].is_on


def test_two_stage_shower_tracks_level_changes():
    shower = DeviceSpec("shower", "Shower", power_levels_w=(3200, 5500))
    loads = [Load(300, 600, 3200), Load(600, 900, 5500)]
    hist = dict(run(aggregate(1400, loads), Disaggregator([shower])))

    assert hist[450.0]["shower"].power_w == 3200
    assert hist[750.0]["shower"].power_w == 5500
    assert not hist[1100.0]["shower"].is_on


def test_state_only_device_learns_its_power_from_toggles():
    ac = DeviceSpec("ac", "Bedroom A/C", has_state=True)
    loads, events = [], []
    for k in range(8):  # eight on/off cycles of an 900 W A/C
        start = 300 + k * 900
        loads.append(Load(start, start + 400, 900))
        events += [(start, "ac", True), (start + 400, "ac", False)]
    hist = run(aggregate(8000, loads), Disaggregator([ac]), events)
    final = hist[-1][1]["ac"]

    assert final.source == "learned_from_state"
    assert final.confidence > 0.5
    mid = dict(hist)[300.0 + 7 * 900 + 100]["ac"]
    assert mid.is_on and mid.power_w == pytest.approx(900, rel=0.05)


def test_state_device_with_unrelated_edge_does_not_learn_it():
    ac = DeviceSpec("ac", "A/C", power_levels_w=(900,), has_state=True)
    # Shower (not declared) steps up 5500 W right as the A/C is switched on.
    samples = aggregate(900, [Load(300, 600, 5500 + 900)])
    hist = run(samples, Disaggregator([ac]), [(300, "ac", True)])
    # The 6400 W step is >50% away from the 900 W prior, so it is rejected.
    assert hist[-1][1]["ac"].confidence <= 0.2
