"""Pairing a rise with its fall: same size, same phases, never one appliance twice at once."""

import pytest
from engine import Discoverer, Edge


def _edge(ts, delta, **channels):
    return Edge(ts, delta, ts, tuple(channels.items()))


def _pairs(disc):
    return sorted(disc._pairs(), key=lambda p: p.start_ts)


def test_a_rise_and_a_fall_that_share_no_phase_are_not_one_run():
    disc = Discoverer()
    disc.add(_edge(0.0, 1500.0, b=1500.0))
    disc.add(_edge(600.0, -1500.0, c=-1500.0))
    assert _pairs(disc) == []


def test_the_fall_takes_the_rise_with_the_same_phases_not_just_the_latest():
    disc = Discoverer()
    disc.add(_edge(0.0, 1500.0, b=1500.0))  # the older one, on the fall's phase
    disc.add(_edge(100.0, 1500.0, c=1500.0))  # more recent, on another phase
    disc.add(_edge(700.0, -1500.0, b=-1500.0))
    (pair,) = _pairs(disc)
    assert pair.start_ts == 0.0 and pair.duration_s == pytest.approx(700.0)


def test_with_equally_good_footprints_the_most_recent_rise_wins():
    disc = Discoverer()
    disc.add(_edge(0.0, 1500.0, b=1500.0))  # its fall was lost
    disc.add(_edge(1000.0, 1500.0, b=1500.0))
    disc.add(_edge(1360.0, -1500.0, b=-1500.0))
    (pair,) = _pairs(disc)
    assert pair.start_ts == 1000.0


def test_steps_without_a_phase_breakdown_still_pair_by_size():
    disc = Discoverer()
    disc.add(Edge(0.0, 900.0, 0.0))
    disc.add(Edge(300.0, -900.0, 300.0))
    assert len(_pairs(disc)) == 1


def test_the_november_12_case_does_not_invent_two_overlapping_runs():
    """Three similar rises in four minutes, and two falls with other phase shares."""
    disc = Discoverer()
    for ts, b, c in ((0.0, 1016.0, 608.0), (106.0, 1097.0, 586.0), (212.0, 1104.0, 566.0)):
        disc.add(_edge(ts, b + c, b=b, c=c))
    disc.add(_edge(4_700.0, -1538.0, b=-719.0, c=-820.0))  # not far from the rises: a pair
    disc.add(_edge(7_800.0, -1735.0, b=-347.0, c=-1388.0))  # mostly another phase: not a pair
    pairs = _pairs(disc)
    assert len(pairs) == 1
    assert pairs[0].start_ts == 212.0


def _group(runs):
    disc = Discoverer()
    for start, end in runs:
        disc.add(Edge(start, 1600.0, start))
        disc.add(Edge(end, -1600.0, end))
    return disc


def test_two_overlapping_runs_of_one_group_are_not_one_appliance():
    # four runs, the last two overlapping: half of them overlap another
    disc = _group([(0, 600), (10_000, 10_600), (20_000, 27_000), (20_120, 24_000)])
    (cand,) = disc.candidates(min_count=2)
    assert cand.overlap_share == pytest.approx(0.5)
    assert not cand.coherent


def test_a_stray_overlap_in_a_big_group_is_tolerated():
    runs = [(k * 10_000, k * 10_000 + 600) for k in range(10)]
    runs.append((10_100, 10_400))  # one run inside another
    (cand,) = _group(runs).candidates(min_count=2)
    assert cand.overlap_share == pytest.approx(2 / 11)
    assert cand.coherent


def test_separate_runs_do_not_overlap():
    (cand,) = _group([(k * 10_000, k * 10_000 + 600) for k in range(5)]).candidates(min_count=2)
    assert cand.overlap_share == 0.0 and cand.coherent
