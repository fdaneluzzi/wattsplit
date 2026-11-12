import pytest
from engine import BaseLoad


def _feed(base, minutes, watts_at, start=0.0):
    for m in range(minutes):
        for s in range(0, 60, 10):  # six readings a minute
            base.add(start + m * 60 + s, watts_at(m))


def test_nothing_is_reported_before_an_hour_of_data():
    base = BaseLoad()
    _feed(base, 30, lambda m: 400.0)
    assert base.value() is None


def test_the_base_load_is_the_low_end_not_the_average():
    base = BaseLoad()
    # busy most of the day (2 kW) with a quiet 8 % of the minutes at 380 W
    _feed(base, 600, lambda m: 380.0 if m % 12 == 0 else 2000.0)
    assert base.value() == pytest.approx(380.0)


def test_a_glitch_shorter_than_a_minute_does_not_set_the_floor():
    base = BaseLoad()
    for m in range(120):
        for s in range(0, 60, 10):
            base.add(m * 60 + s, 1500.0 if s else 0.0)  # one zero reading per minute
    assert base.value() == pytest.approx(1250.0)  # the mean of the minute, not the zero


def test_old_minutes_leave_the_window():
    base = BaseLoad(window_s=7200)
    _feed(base, 120, lambda m: 300.0)
    _feed(base, 130, lambda m: 900.0, start=7200.0)
    assert base.value() == pytest.approx(900.0)


def test_missing_readings_are_ignored():
    base = BaseLoad()
    base.add(0.0, None)
    assert base.value() is None
