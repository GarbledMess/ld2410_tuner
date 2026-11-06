"""Row budgets read a day's geometry once: the same count as the rows, without their decoding."""

import random
from array import array

import pytest
import test_tuner  # noqa: F401
from room_speed_fixture import DAY, START, build, request
from tuner_under_test.engine.hybrid.adapters import _WindowRows
from tuner_under_test.engine.hybrid.physical_index import PHYSICAL_BLOCK_ROWS, PhysicalRows
from tuner_under_test.history import day_results, fit_days, worker_snapshot
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
    fixture = build(root, days=4, open_cells=600, sensor_days=2)
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
    source = CapturedRowSource(views["radar_a"], "radar_a", (0, 1, 5, 17))
    checked = CapturedRowSource(views["radar_a"], "radar_a", (0, 1, 5, 17))
    for lo, hi in _windows(1):
        assert source.rows_in(lo, hi) == legacy_rows_in(checked, lo, hi)


def test_count_equals_the_row_walk_through_a_clipped_source(views):
    view = views["radar_b"]
    source = CapturedRowSource(view, "radar_b", (2, 3))
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
    windows = _windows(3)
    for _ in range(3):  # Each plan builds its own source over the job's history.
        source = CapturedRowSource(views["radar_a"], "radar_a", channels)
        for lo, hi in windows:
            source.rows_in(lo, hi)
    assert len(calls) == len(set(calls)) <= 4


def test_kept_geometry_is_bounded_and_evicts_oldest():
    kept = fit_days._GeometryCache(limit=64 * 1024)
    rng = random.Random(4)
    for number in range(40):
        times = array("d", (rng.random() + at for at in range(2000)))
        ends = array("d", (at + 0.5 for at in times))
        kept.put(number, times, ends)
    assert kept.size <= 64 * 1024
    assert kept.get(0) is None
    stored = kept.get(39)
    assert stored == (times, ends)


def test_geometry_bigger_than_the_limit_is_not_kept():
    kept = fit_days._GeometryCache(limit=8)
    kept.put("day", array("d", (1.0, 2.0)), array("d", (2.0, 3.0)))
    assert kept.get("day") is None and kept.size == 0


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
