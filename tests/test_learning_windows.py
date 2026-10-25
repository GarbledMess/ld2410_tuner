"""Windowed physical reads preserve held values without traversing excluded rows."""

from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import test_tuner  # noqa: F401
from tuner_under_test.history import dayfile, spans


def fixture():
    day = dayfile.day_from_log(
        [
            dayfile.Change(0, {0: 10}),
            dayfile.Change(10, {0: 30}),
            dayfile.End(20),
            dayfile.Change(30, {0: None, 1: 7.25}),
            dayfile.End(50),
        ],
        None,
        0,
        86400,
    )
    store = SimpleNamespace(
        days=lambda key: [date(1970, 1, 1)], day=lambda *a, **kw: day, live_date=lambda *a: None
    )
    return SimpleNamespace(_days=store)


def clipped(items, windows, low, high):
    return [
        spans.Span(max(cell.start, a, low), min(cell.end, b, high), cell.values, cell.mode)
        for cell in items
        for a, b in windows
        if min(cell.end, b, high) > max(cell.start, a, low)
    ]


@pytest.mark.parametrize(
    "windows",
    [[(5, 15)], [(1, 3), (13, 16), (25, 35), (40, 60)], [], [(15, 35)], [(0, 10), (10, 20)]],
)
def test_selected_windows_equal_clipped_full_trace(windows):
    view = fixture()
    original = list(spans.events(view, "a"))
    view._learning_windows = {"a": windows}
    expected_windows = [(0, 20)] if windows == [(0, 10), (10, 20)] else windows
    expected = clipped(original, expected_windows, 0, 50)
    assert list(spans.events(view, "a")) == expected


def test_window_edges_intersect_query_physical_attribution_and_frozen_bounds():
    view = fixture()
    original = list(spans.events(view, "a"))
    view._learning_windows = {"a": [(0, 50)]}
    view._physical_history_start = 3
    view._attribution_until = 45
    view._history_until = 35
    assert list(spans.events(view, "a", start=5, end=40)) == clipped(original, [(0, 50)], 5, 35)


def test_unselected_device_keeps_its_original_trace():
    view = fixture()
    original = list(spans.events(view, "other"))
    view._learning_windows = {"a": [(5, 7)]}
    assert list(spans.events(view, "other")) == original


def test_overlapping_and_adjacent_windows_never_double_count():
    view = fixture()
    view._learning_windows = {"a": [(15, 18), (5, 12), (10, 15)]}
    assert [(cell.start, cell.end) for cell in spans.events(view, "a")] == [(5, 10), (10, 18)]


@pytest.mark.parametrize("window", [(5, 4), (float("nan"), 5), (0, float("inf"))])
def test_malformed_selection_is_rejected(window):
    view = fixture()
    view._learning_windows = {"a": [window]}
    with pytest.raises(ValueError):
        list(spans.events(view, "a"))


def test_narrow_windows_bisect_large_day_without_replaying_excluded_vectors():
    records = [dayfile.Change(i, {0: i % 100}) for i in range(20000)] + [dayfile.End(20000)]
    day = dayfile.day_from_log(records, None, 0, 86400)
    store = SimpleNamespace(
        days=lambda key: [date(1970, 1, 1)], day=lambda *a, **kw: day, live_date=lambda *a: None
    )
    view = SimpleNamespace(
        _days=store, _learning_windows={"a": [(50.25, 52.5), (19000.5, 19001.5)]}
    )
    original_vectors, read = dayfile.Day.vectors, []

    def vectors(self, first, after):  # The rows each window's spans read.
        read.append(after - first)
        return original_vectors(self, first, after)

    with patch.object(dayfile.Day, "vectors", vectors):
        found = list(spans.events(view, "a"))
    assert len(found) == 5
    assert sum(read) == 5
    assert sum(cell.end - cell.start for cell in found) == 3.25


def test_empty_selection_does_not_load_any_day():
    view = fixture()
    view._learning_windows = {"a": []}
    with patch.object(view._days, "day", side_effect=AssertionError("unselected day read")):
        assert list(spans.events(view, "a")) == []


def test_carry_is_clipped_to_selected_context_without_filling_gap():
    carry = dayfile.Carry((40,) + (None,) * 17, None)
    day = dayfile.day_from_log(
        [dayfile.End(5), dayfile.Change(12, {0: 8}), dayfile.End(20)], carry, 0, 86400
    )
    view = SimpleNamespace(
        _days=SimpleNamespace(
            days=lambda key: [date(1970, 1, 1)], day=lambda *a, **kw: day, live_date=lambda *a: None
        ),
        _learning_windows={"a": [(3, 14)]},
    )
    found = list(spans.events(view, "a"))
    assert [(cell.start, cell.end, cell.values[0]) for cell in found] == [(3, 5, 40), (12, 14, 8)]


def test_day_loading_skips_dates_outside_selected_windows():
    first = dayfile.day_from_log([dayfile.Change(0, {0: 10}), dayfile.End(100)], None, 0, 86400)
    second = dayfile.day_from_log(
        [dayfile.Change(86400, {0: 40}), dayfile.End(86500)], None, 86400, 172800
    )
    days = {date(1970, 1, 1): first, date(1970, 1, 2): second}
    loaded = []

    def load(key, date, **kwargs):
        loaded.append(date)
        return days[date]

    view = SimpleNamespace(
        _days=SimpleNamespace(days=lambda key: list(days), day=load, live_date=lambda *a: None),
        _learning_windows={"a": [(86405, 86415)]},
    )
    found = list(spans.events(view, "a"))
    assert [(cell.start, cell.end, cell.values[0]) for cell in found] == [(86405, 86415, 40)]
    assert loaded == [date(1970, 1, 2)]


def test_query_inside_one_tiny_window_keeps_exact_boundaries():
    view = fixture()
    view._learning_windows = {"a": [(5.000001, 5.000004), (15, 16)]}
    found = list(spans.events(view, "a", start=5.000002, end=5.000003))
    assert [(cell.start, cell.end, cell.values[0]) for cell in found] == [(5.000002, 5.000003, 10)]


def test_zero_length_context_is_empty_and_missing_day_is_not_invented():
    view = fixture()
    view._learning_windows = {"a": [(5, 5)]}
    assert list(spans.events(view, "a")) == []
    view._learning_windows = {"a": [(5, 8)]}
    view._days.day = lambda *a, **kw: None
    assert list(spans.events(view, "a")) == []


@pytest.mark.parametrize("windows", [None, [], [(5, 15)], [(18, 35), (42, 45)], [(21, 29)]])
def test_column_bounds_equal_independent_selected_span_oracle(windows):
    view = fixture()
    if windows is not None:
        view._learning_windows = {"a": windows}
    selected = list(spans.events(view, "a", start=2, end=46))
    expected = (selected[0].start, selected[-1].end) if selected else (0, 0)
    with patch.object(dayfile.Day, "vector", side_effect=AssertionError("bounds replayed vectors")):
        assert spans.bounds(view, "a", 2, 46) == expected


def test_column_bounds_honor_all_snapshot_limits_without_vectors():
    view = fixture()
    view._physical_history_start = 3
    view._attribution_until = 45
    view._history_until = 35
    with patch.object(dayfile.Day, "vector", side_effect=AssertionError("bounds replayed vectors")):
        assert spans.bounds(view, "a", 0, 50) == (3, 35)


def test_column_bounds_skip_zero_duration_boundary_rows():
    day = SimpleNamespace(times=[0, 5, 10, 15], ends=[0, 10, 15, 15])
    view = SimpleNamespace(
        _days=SimpleNamespace(
            days=lambda key: [date(1970, 1, 1)], day=lambda *a, **kw: day, live_date=lambda *a: None
        )
    )
    assert spans.bounds(view, "a", 0, 20) == (5, 15)
    assert spans.bounds(view, "a", 10, 10) == (0, 0)


def test_column_bounds_missing_day_and_missing_store_are_empty():
    view = fixture()
    view._days.day = lambda *a, **kw: None
    assert spans.bounds(view, "a", 0, 100) == (0, 0)
    assert spans.bounds(SimpleNamespace(), "a", 0, 100) == (0, 0)
