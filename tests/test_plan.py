import pytest
from engine import (
    Sources,
    channel_values,
    choose_plan,
    consumption_w,
)


def periods(**by_sensor):
    return lambda sensor: by_sensor.get(sensor)


def reader(**values):
    return lambda sensor: values.get(sensor)


def test_slow_inverter_next_to_a_fast_meter_becomes_evidence_not_part_of_detection():
    src = Sources(grid=("grid1",), generation=("inv1",))
    plan = choose_plan(src, periods(grid1=15, inv1=61))

    assert plan.kind == "grid" and plan.detect == ("grid",)
    assert plan.sensors_of("grid") == ("grid1",)
    assert plan.evidence == ("inv1",)


def test_equally_fast_generation_is_composed_into_the_detection_signal():
    src = Sources(grid=("grid1",), generation=("inv1",), battery=("bat",))
    plan = choose_plan(src, periods(grid1=15, inv1=15, bat=20))

    assert plan.kind == "composed" and plan.detect == ("composed",)
    assert plan.sensors_of("composed") == ("grid1", "inv1", "bat")
    assert plan.evidence == ()  # already inside the signal


def test_unknown_rates_do_not_compose_until_they_are_measured():
    src = Sources(grid=("grid1",), generation=("inv1",))
    assert choose_plan(src, periods()).kind == "grid"


def test_several_solar_systems_and_a_battery_are_all_evidence():
    src = Sources(grid=("grid1",), generation=("inv1", "inv2"), battery=("bat",))
    plan = choose_plan(src, periods(grid1=15, inv1=61, inv2=120, bat=30))

    assert plan.kind == "grid"
    assert set(plan.evidence) == {"inv1", "inv2", "bat"}


def test_two_utility_meters_are_two_channels():
    src = Sources(grid=("meterA", "meterB"))
    plan = choose_plan(src, periods(meterA=15, meterB=15))

    assert plan.detect == ("meterA", "meterB")
    assert plan.sensors_of("meterB") == ("meterB",)


def test_load_sensors_are_the_channels_and_evidence_is_opt_in():
    src = Sources(load=("a", "b", "c"), generation=("inv1",))
    assert choose_plan(src, periods()).detect == ("a", "b", "c")
    assert choose_plan(src, periods()).evidence == ()
    assert choose_plan(src, periods(), evidence=["inv1"]).evidence == ("inv1",)
    # a sensor already inside the detection signal is never also evidence
    assert choose_plan(src, periods(), evidence=["a", "inv1"]).evidence == ("inv1",)


def test_a_ready_made_house_sensor_is_a_single_channel():
    plan = choose_plan(Sources(house="casa", grid=("g",)), periods())
    assert plan.kind == "house" and plan.detect == ("house",)


def test_consumption_is_rebuilt_from_grid_generation_and_battery():
    src = Sources(grid=("g",), generation=("s1", "s2"), battery=("b",))
    read = reader(g=-800.0, s1=1500.0, s2=900.0, b=-200.0)  # exporting, battery charging
    assert consumption_w(src, read) == pytest.approx(1400.0)


def test_inverted_grid_meters_are_corrected():
    src = Sources(grid=("g",), generation=("s",), invert_grid=True)
    assert consumption_w(src, reader(g=-300.0, s=1000.0)) == pytest.approx(1300.0)


def test_consumption_prefers_house_then_load_sum_and_waits_for_missing_sensors():
    assert consumption_w(Sources(house="h", load=("a",)), reader(h=700.0, a=1.0)) == 700.0
    assert consumption_w(Sources(load=("a", "b")), reader(a=100.0, b=250.0)) == 350.0
    assert consumption_w(Sources(load=("a", "b")), reader(a=100.0)) is None
    assert consumption_w(Sources(grid=("g",), generation=("s",)), reader(g=5.0)) is None


def test_channel_values_skip_unavailable_channels_and_correct_signs():
    src = Sources(load=("a", "b"))
    plan = choose_plan(src, periods())
    assert channel_values(plan, src, reader(a=120.0)) == {"a": 120.0}

    src = Sources(grid=("g1", "g2"), invert_grid=True)
    plan = choose_plan(src, periods())
    assert channel_values(plan, src, reader(g1=-400.0, g2=250.0)) == {"g1": 400.0, "g2": -250.0}

    src = Sources(grid=("g",), generation=("s",))
    plan = choose_plan(src, periods(g=15, s=15))
    assert plan.kind == "composed"
    assert channel_values(plan, src, reader(g=-200.0, s=900.0)) == {"composed": 700.0}
