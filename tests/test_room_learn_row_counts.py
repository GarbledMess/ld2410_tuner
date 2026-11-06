"""Row budgets read a day's geometry once: the same count as the rows, without their decoding."""

import random
from array import array

import pytest
import test_tuner  # noqa: F401
from room_speed_fixture import DAY, START, build, request
from tuner_under_test.engine.hybrid.adapters import _WindowRows
from tuner_under_test.engine.hybrid.physical_index import PHYSICAL_BLOCK_ROWS, PhysicalRows
from tuner_under_test.history import day_results, daystore, fit_days, worker_snapshot
from tuner_under_test.history.fit_rows import CapturedRowSource
from tuner_under_test.worker import calculations


def legacy_rows_in(source, lo, hi):
    """The count this replaced: every day's physical rows, read per window."""
    total = 0
    for date, a, b in source._windows(lo, hi):
        total += (
            fit_days.FittingDays(source.view)
            .physical(source.device_id, date, source.channels)
            .rows_in(a, b)
        )
    return total


def brute_rows_in(rows, lo, hi):
    times, ends = rows.geometry()
    return sum(1 for a, b in zip(times, ends, strict=True) if a < hi and b > lo) if hi > lo else 0


@pytest.fixture(scope="module")
def views(tmp_path_factory, monkeypatch_module):
    monkeypatch_module.setattr(worker_snapshot, "_executor_only", lambda: None)
    root = tmp_path_factory.mktemp("rows")
    fixture = build(root, days=4, open_seconds=3600, sensor_days=2)
    captured = request(fixture, root / "job", window_days=3, attribution_days=2)
    from tuner_under_test.worker.manifest import expand

    expanded = expand(captured)
    shared = calculations.JobHistory()
    return {key: calculations.restore(value, shared) for key, value in expanded["views"].items()}


@pytest.fixture(scope="module")
def monkeypatch_module():
    with pytest.MonkeyPatch.context() as patch:
        yield patch


def _windows(seed):
    rng = random.Random(seed)
    span = 4 * DAY
    found = [
        (START + rng.randrange(0, span * 8) / 8, rng.randrange(0, 40000) / 1.6) for _ in range(150)
    ]
    found = [(low, low + length) for low, length in found]
    found += [(START, START + span), (START - 50, START), (START + 6, START + 6)]
    found += [(START + DAY, START + 2 * DAY), (START + 12, START + 6)]
    return found


def test_count_equals_the_row_walk_on_every_window(views):
    source = CapturedRowSource(views["radar_a"], "radar_a", (0, 1, 5, 9))
    checked = CapturedRowSource(views["radar_a"], "radar_a", (0, 1, 5, 9))
    for lo, hi in _windows(1):
        assert source.rows_in(lo, hi) == legacy_rows_in(checked, lo, hi)


def test_count_equals_the_row_walk_through_a_clipped_source(views):
    view = views["radar_b"]
    source = CapturedRowSource(view, "radar_b", (0, 1))
    clipped = _WindowRows(source, START + 7.5, START + 3 * DAY + 11)
    for lo, hi in _windows(2):
        a, b = max(lo, clipped.start), min(hi, clipped.end)
        assert clipped.rows_in(lo, hi) == legacy_rows_in(source, a, b)


def test_many_windows_read_each_day_once_across_passes(views, monkeypatch):
    calls = []
    physical = fit_days.FittingDays.physical

    def counted(self, device_id, date, channels):
        calls.append((device_id, date))
        return physical(self, device_id, date, channels)

    monkeypatch.setattr(fit_days.FittingDays, "physical", counted)
    channels = (0, 4, 9)  # Not used by another test: nothing of this geometry is kept yet.
    # Disjoint and in order, as a plan's regions are; many in each day.
    windows = [(START + 1000.5 * at, START + 1000.5 * at + 400.25) for at in range(340)]
    for _ in range(3):  # Each plan builds its own source over the job's history.
        source = CapturedRowSource(views["radar_a"], "radar_a", channels)
        for lo, hi in windows:
            source.rows_in(lo, hi)
    assert len(calls) == len(set(calls)) <= 4


def test_one_job_counts_are_not_shared_with_another_channel_set(views):
    wide = CapturedRowSource(views["radar_a"], "radar_a", (0, 1))
    narrow = CapturedRowSource(views["radar_a"], "radar_a", (0, 1, 2, 3, 4, 5, 6, 7, 8, 9))
    window = START + 6, START + 3600
    assert wide.rows_in(*window) == legacy_rows_in(wide, *window)
    assert narrow.rows_in(*window) == legacy_rows_in(narrow, *window)
    assert wide.rows_in(*window) != 0


def test_an_earlier_view_does_not_reuse_a_later_cut_of_the_same_day(views):
    from copy import copy

    view = views["radar_a"]
    later = CapturedRowSource(view, "radar_a", (0, 1, 2))
    window = START + 6, START + 4 * DAY
    assert later.rows_in(*window) == legacy_rows_in(later, *window)
    earlier_view = copy(view)
    earlier_view._history_until = START + 3 * DAY + 600
    earlier = CapturedRowSource(earlier_view, "radar_a", (0, 1, 2))
    assert earlier.rows_in(*window) == legacy_rows_in(earlier, *window)
    assert earlier.rows_in(*window) < later.rows_in(*window)


def test_rows_geometry_spans_blocks_and_agrees_with_rows_in():
    rows_count = PHYSICAL_BLOCK_ROWS + 100
    times = array("d", (at * 2.0 for at in range(rows_count)))
    ends = array("d", (at + 1.5 for at in times))
    rows = PhysicalRows((0,), times, ends, {0: bytes(rows_count)})
    got_times, got_ends = rows.geometry()
    assert (got_times, got_ends) == (times, ends)
    for lo, hi in ((0, 10), (32000, 33000), (3.5, 3.75), (1.5, 2), (70000, 80000), (9, 3)):
        assert rows.rows_in(lo, hi) == brute_rows_in(rows, lo, hi)


def test_day_results_stay_untouched_by_counting(views):
    before = day_results.kept_bytes()
    CapturedRowSource(views["radar_b"], "radar_b", (1,)).rows_in(START, START + DAY)
    assert day_results.kept_bytes() == before


def test_a_window_looks_only_at_the_days_it_reaches(views, monkeypatch):
    calls, plain = [], daystore.bounds

    def counted(date):
        calls.append(date)
        return plain(date)

    monkeypatch.setattr(daystore, "bounds", counted)
    source = CapturedRowSource(views["radar_a"], "radar_a", (0,))
    dates = list(source.view._days.days("radar_a"))
    assert len(dates) == 4
    found = list(source._windows(START + 3 * DAY + 5, START + 3 * DAY + 10))
    assert len(calls) <= 2
    assert [item[0] for item in found] == dates[3:]
    assert [item[0] for item in source._windows(float("-inf"), float("inf"))] == dates
    assert list(source._windows(START - 2 * DAY, START - DAY)) == []
    assert [item[0] for item in source._windows(START + DAY, START + 2 * DAY)] == dates[1:2]
