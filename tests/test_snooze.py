import pytest
from engine import Discoverer, Edge, Snoozes

DAY = 86_400.0


def _candidate(starts, level=1340.0, duration=360.0):
    disc = Discoverer()
    for t in starts:
        disc.add(Edge(t, level, t))
        disc.add(Edge(t + duration, -level, t + duration))
    (cand,) = disc.candidates(min_count=2)
    return cand


def test_a_skipped_load_stays_out_until_it_runs_again():
    first = _candidate([1_000_000 + d * DAY for d in range(4)])
    snoozes = Snoozes()
    skipped_at = 1_000_000 + 4 * DAY
    snoozes.add(1340.0, 360.0, skipped_at)

    assert snoozes.hides(first, skipped_at + 3600)
    assert snoozes.visible([first], skipped_at + 3600) == []

    # it runs again after the skip: back in the inbox, with the new run in its list
    again = _candidate([1_000_000 + d * DAY for d in range(4)] + [skipped_at + 7200])
    assert not snoozes.hides(again, skipped_at + 10_800)
    assert again.recent[0][0] == pytest.approx(skipped_at + 7200)


def test_once_it_is_back_the_skip_is_forgotten():
    snoozes = Snoozes()
    snoozes.add(1340.0, 360.0, 1_000_000.0)
    later = _candidate([1_000_000.0 + 3600, 1_000_000.0 + 7200])
    assert not snoozes.hides(later, 1_000_000.0 + 8000)
    snoozes.changed = False
    # a second look does not find a stale record to hide the next appearance
    assert not snoozes.hides(later, 1_000_000.0 + 9000)
    assert snoozes.as_list() == []


def test_a_different_load_is_not_hidden_by_the_skip_of_another():
    snoozes = Snoozes()
    snoozes.add(1340.0, 360.0, 2_000_000.0)
    other_power = _candidate([1_000_000 + d * DAY for d in range(4)], level=2400.0)
    other_length = _candidate([1_000_000 + d * DAY for d in range(4)], duration=9_000.0)
    assert not snoozes.hides(other_power, 2_000_100.0)
    assert not snoozes.hides(other_length, 2_000_100.0)


def test_a_skip_expires_after_sixty_days():
    cand = _candidate([1_000_000 + d * DAY for d in range(4)])
    snoozes = Snoozes()
    snoozes.add(1340.0, 360.0, 2_000_000.0)
    assert snoozes.hides(cand, 2_000_000.0 + 30 * DAY)
    assert not snoozes.hides(cand, 2_000_000.0 + 61 * DAY)


def test_skips_survive_a_save_and_a_damaged_record_is_ignored():
    snoozes = Snoozes()
    snoozes.add(1340.0, 360.0, 5.0)
    restored = Snoozes.from_list(snoozes.as_list() + [{"level_w": "bad"}, {}])
    assert restored.as_list() == snoozes.as_list()


def test_skipping_the_same_load_twice_keeps_one_record():
    snoozes = Snoozes()
    snoozes.add(1340.0, 360.0, 1.0)
    snoozes.add(1350.0, 400.0, 2.0)
    assert len(snoozes.as_list()) == 1 and snoozes.as_list()[0]["at"] == 2.0
