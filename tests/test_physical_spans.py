"""One native physical timeline preserves boundaries, predecessor values and gaps."""

from types import SimpleNamespace

import test_tuner  # noqa: F401
from tuner_under_test.history import dayfile, daystore, spans


def vector(value):
    return (value,) + (None,) * 17


def view(tmp_path, blocks=(), *, until=None):
    days = daystore.DayStore(tmp_path / "history")
    for events, end in blocks:
        for timestamp, values in events:
            days.append("radar-any-name", dayfile.Change(timestamp, dict(enumerate(values))))
        if end is not None:
            days.append("radar-any-name", dayfile.End(end))
    return SimpleNamespace(
        data={"devices": {"radar-any-name": {"recording_enabled": True}}},
        _days=days,
        _history_until=until,
    )


def coordinates(items):
    return [(item.start, item.end, item.values[0], item.mode) for item in items]


def test_event_spans_preserve_pulse_and_query_predecessor(tmp_path):
    block = ([(0, vector(0)), (1.125, vector(40)), (1.175, vector(0))], 60)
    assert coordinates(
        spans.events(view(tmp_path, [block]), "radar-any-name", start=1.15, end=2)
    ) == [
        (1.15, 1.175, 40, "event"),
        (1.175, 2, 0, "event"),
    ]


def test_exact_duration_spans_retain_explicit_recording_gaps(tmp_path):
    runtime = view(
        tmp_path,
        [([(0, vector(10)), (6, vector(20)), (12, vector(30))], 15), ([(100, vector(40))], 103)],
    )
    assert coordinates(spans.events(runtime, "radar-any-name")) == [
        (0, 6, 10, "event"),
        (6, 12, 20, "event"),
        (12, 15, 30, "event"),
        (100, 103, 40, "event"),
    ]


def test_separate_recordings_do_not_fill_the_gap_before_resuming(tmp_path):
    runtime = view(tmp_path, [([(0, vector(10)), (3, vector(20))], 6), ([(10, vector(40))], 20)])
    found = list(spans.events(runtime, "radar-any-name"))
    assert coordinates(found) == [
        (0, 3, 10, "event"),
        (3, 6, 20, "event"),
        (10, 20, 40, "event"),
    ]
    assert spans.coverage(found) == [(0, 6), (10, 20)]


def test_open_coverage_is_visible_without_encoding_or_flushing(tmp_path):
    runtime = view(tmp_path, [([(10, vector(40))], None)], until=3610)
    assert coordinates(spans.events(runtime, "radar-any-name")) == [(10, 3610, 40, "event")]
    assert runtime._days.current("radar-any-name") == vector(40)


def test_unavailable_span_remains_an_explicit_coverage_gap(tmp_path):
    block = ([(0, vector(40)), (10, vector(None)), (50, vector(40))], 60)
    found = list(spans.events(view(tmp_path, [block]), "radar-any-name"))
    assert spans.coverage(found) == [(0, 10), (50, 60)]
    assert spans.coverage(found, gate=1) == []


def test_chronological_view_never_observes_future_energy_or_coverage(tmp_path):
    earlier = view(tmp_path, [([(0, vector(10)), (20, vector(99)), (30, vector(40))], 50)])
    earlier._attribution_until = 15
    assert coordinates(spans.events(earlier, "radar-any-name")) == [(0, 15, 10, "event")]


def test_window_clips_predecessor_and_successor_without_overreading(tmp_path):
    bounded = view(tmp_path, [([(0, vector(10)), (3, vector(20)), (6, vector(30))], 10)])
    assert coordinates(spans.events(bounded, "radar-any-name", start=2, end=4)) == [
        (2, 3, 10, "event"),
        (3, 4, 20, "event"),
    ]


def test_retention_clips_native_predecessor_without_reimporting_expired_coverage(tmp_path):
    block = ([(0, vector(40)), (80, vector(10))], 100)
    retained = view(tmp_path, [block])
    retained._physical_history_start = 50
    assert coordinates(spans.events(retained, "radar-any-name")) == [
        (50, 80, 40, "event"),
        (80, 100, 10, "event"),
    ]
    assert not spans.has_events(retained, "radar-any-name", end=40)


def reference_spans(day, start, end):
    """``day_spans`` as read one row at a time (``Day.vector``): what any faster read gives."""
    return [
        (max(start, low), min(end, high), day.vector(index), "event")
        for index, (low, high) in enumerate(zip(day.times, day.ends, strict=True))
        if min(end, high) > max(start, low)
    ]


def test_day_spans_read_every_row_as_its_vector_with_missing_readings_and_clipping():
    import math
    from array import array

    rows = 300
    times = array("d", (i * 2.0 for i in range(rows)))
    ends = array("d", (t + 1.5 for t in times))
    columns = [bytes((i * (g + 3) + g) % 256 for i in range(rows)) for g in range(18)]
    native = dayfile.Day(0.0, 86400.0, times, ends, columns, [])
    legacy = dayfile.Day(  # Legacy float columns: NaN is a missing reading.
        0.0,
        86400.0,
        times,
        ends,
        [array("d", (math.nan if v == 255 else v / 2 for v in c)) for c in columns],
        [],
    )
    assert any(255 in column for column in columns), "rows with missing readings are covered"
    for day in (native, legacy, dayfile.Day(0.0, 86400.0, times, ends, [], [])):
        for start, end in ((-math.inf, math.inf), (101.25, 403.0), (0.0, 0.0), (999.0, 1e9)):
            found = [(s.start, s.end, s.values, s.mode) for s in spans.day_spans(day, start, end)]
            assert found == reference_spans(day, start, end)
