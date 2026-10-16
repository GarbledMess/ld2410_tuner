"""A bounded, representative evidence budget: the most valuable recorded time is kept whole,
the rest is sampled evenly and scaled so each class keeps its total time."""

import datetime

import pytest
import test_tuner  # noqa: F401
from tuner_under_test.calibration import evidence_budget as budget
from tuner_under_test.history import dayfile, daystore

Chunk = budget.Chunk
START = 1_760_486_400.0


def chunk(i, state="present", rows=100, peak=50, floor=10, edge=False, minutes=10, seconds=None):
    start = START + i * 600
    seconds = minutes * 60 if seconds is None else seconds
    return Chunk(start, start + minutes * 60, state, rows, peak, floor, edge, seconds)


def kept(windows):
    return [(w.start, w.end) for w in windows]


def test_everything_fits_so_everything_is_kept_at_its_own_weight():
    chunks = [chunk(i) for i in range(5)]
    windows = budget.select(chunks, limit=10_000)
    assert kept(windows) == [(c.start, c.end) for c in chunks]
    assert all(w.scale == 1 for w in windows)


def test_each_class_keeps_its_total_time_when_sampled():
    chunks = [chunk(i, "not_present", floor=0, peak=5) for i in range(100)]
    chunks += [chunk(100 + i, "present", floor=40, peak=60) for i in range(100)]
    windows = budget.select(chunks, limit=4_000)
    assert sum(c.rows for c in chunks if (c.start, c.end) in kept(windows)) <= 4_000
    for state in ("present", "not_present"):
        total = sum(c.end - c.start for c in chunks if c.state == state)
        stood_for = sum((w.end - w.start) * w.scale for w in windows if w.state == state)
        assert stood_for == pytest.approx(total)


def test_label_edges_newest_time_and_extremes_are_always_kept_whole():
    chunks = [chunk(i, "not_present", peak=5) for i in range(200)]
    chunks[17] = chunk(17, "not_present", peak=95)  # The loudest empty-room noise.
    chunks[60] = chunk(60, "not_present", peak=5, edge=True)  # Beside a label edge.
    chunks += [chunk(200 + i, "present", floor=40, peak=60) for i in range(200)]
    chunks[300] = chunk(300, "present", floor=3, peak=8)  # The quietest presence.
    windows = budget.select(chunks, limit=6_000)
    by_start = {w.start: w for w in windows}
    for index in (17, 60, 300, 199, 399):  # Noise, edge, quiet presence, newest of each.
        assert by_start[chunks[index].start].scale == 1, index


def test_sampling_is_deterministic_and_spread_over_the_whole_history():
    chunks = [chunk(i, "not_present", peak=5) for i in range(300)]
    first = budget.select(chunks, limit=3_000)
    assert budget.select(chunks, limit=3_000) == first
    starts = sorted(w.start for w in first)
    span = chunks[-1].start - chunks[0].start
    assert starts[0] - chunks[0].start < span / 5  # Old time is represented too.


def test_budget_counts_rows_not_time():
    dense = [chunk(i, "present", rows=5_000) for i in range(10)]
    windows = budget.select(dense, limit=12_000)
    assert sum(5_000 for _ in windows) <= 12_000 + 5_000  # At most one chunk over.


def test_chunks_of_a_day_cut_at_label_edges_and_summarise_columns(tmp_path):
    store = daystore.DayStore(tmp_path / "history")
    records = [dayfile.Change(START + i * 6, {0: i % 50, 1: 7}) for i in range(1200)]
    records.append(dayfile.End(START + 7200))
    day = dayfile.day_from_log(records, None, START, START + 86_400)
    store.replace("radar", datetime.date(2025, 10, 15), day)
    labels = {"history_labels": [{"start": START, "end": START + 3000, "state": "present"}]}
    from tuner_under_test.history.label_lookup import _history_label_reader

    found = budget.chunks_of(day, _history_label_reader(labels), [0, 1], minutes=10)
    assert found[0].start == START and found[0].state == "present"
    assert any(c.edge for c in found)  # The chunk ending at the label edge.
    assert sum(c.rows for c in found) == 1200
    assert max(c.peak for c in found) == 49 and min(c.floor for c in found) == 0
    assert {c.state for c in found} == {"present", None}  # Unlabelled after the label.


def test_valuable_time_itself_is_bounded_newest_and_extremes_first():
    """Dense recordings with many labels: edges alone could exceed the budget."""
    chunks = [chunk(i, "not_present", rows=1_000, peak=5, edge=True) for i in range(50)]
    chunks[3] = chunk(3, "not_present", rows=1_000, peak=95, edge=True)  # Loudest noise.
    windows = budget.select(chunks, limit=10_000)
    rows = sum(1_000 for w in windows if w.scale == 1)
    assert rows <= 10_000
    starts = {w.start for w in windows}
    assert chunks[3].start in starts and chunks[-1].start in starts  # Extreme and newest.
    assert chunks[-2].start in starts  # Then edges, newest first.
    covered = sum((w.end - w.start) * w.scale for w in windows)
    assert covered == pytest.approx(sum(c.end - c.start for c in chunks))


def test_label_edges_get_short_chunks_of_their_own():
    from tuner_under_test.history.label_lookup import _history_label_reader

    records = [dayfile.Change(START + i * 2, {0: i % 50}) for i in range(3600)]
    day = dayfile.day_from_log([*records, dayfile.End(START + 7200)], None, START, START + 86_400)
    labels = {"history_labels": [{"start": START, "end": START + 3000, "state": "present"}]}
    found = budget.chunks_of(day, _history_label_reader(labels), [0])
    edges = [c for c in found if c.edge]
    assert edges and all(c.end - c.start <= 2 * budget.EDGE_SECONDS for c in edges)
    assert max(c.end - c.start for c in found) <= budget.CHUNK_MINUTES * 60


def day_of(*records, end):
    return dayfile.day_from_log([*records, dayfile.End(end)], None, START, START + 86_400)


NO_LABELS = type("Read", (), {"edges": (), "__call__": lambda self, t: None})()


def test_a_reading_held_across_a_chunk_is_in_every_chunk_it_covers():
    day = day_of(
        dayfile.Change(START, {0: 10}), dayfile.Change(START + 1500, {0: 20}), end=START + 1800
    )
    found = budget.chunks_of(day, NO_LABELS, [0])
    middle = next(c for c in found if c.start == START + 600)  # No change starts in it.
    assert (middle.rows, middle.seconds, middle.peak) == (1, 600, 10)
    assert sum(c.seconds for c in found) == 1800


def test_chunk_time_is_observed_time_not_recording_gaps():
    day = day_of(
        dayfile.Change(START, {0: 10}),
        dayfile.End(START + 100),
        dayfile.Change(START + 400, {0: 12}),
        end=START + 500,
    )
    (only,) = budget.chunks_of(day, NO_LABELS, [0])
    assert only.seconds == 200


def test_missing_readings_are_not_observations():
    day = day_of(
        dayfile.Change(START, {0: None, 1: None}),
        dayfile.Change(START + 700, {0: 30}),
        end=START + 1000,
    )
    found = budget.chunks_of(day, NO_LABELS, [0, 1])
    assert [(c.start, c.seconds, c.peak) for c in found] == [(START + 600, 300, 30)]
    unread = Chunk(START, START + 600, "present", 5, None, None, False, 0)
    windows = budget.select([unread, *(chunk(i + 1, rows=500) for i in range(9))], limit=1_000)
    assert windows  # No TypeError comparing a missing peak.


def test_sampled_classes_keep_their_observed_time():
    chunks = [chunk(i, "not_present", peak=5, seconds=100 + i) for i in range(100)]
    windows = budget.select(chunks, limit=2_000)
    seconds = {c.start: c.seconds for c in chunks}
    stood_for = sum(seconds[w.start] * w.scale for w in windows)
    assert stood_for == pytest.approx(sum(c.seconds for c in chunks))


def test_no_chunk_holds_more_than_the_row_cap():
    day = day_of(
        *(dayfile.Change(START + i / 2, {0: i % 50}) for i in range(1, 3000)), end=START + 1600
    )
    found = budget.chunks_of(day, NO_LABELS, [0])
    assert max(c.rows for c in found) <= budget.MAX_CHUNK_ROWS + 1  # Plus a held-over row.
    assert sum(c.seconds for c in found) == pytest.approx(1600 - 0.5)


def test_strata_may_carry_provenance_with_the_class_first():
    """A stratum key such as ("present", "automatic", 80) keeps its class's extremes."""
    quiet = chunk(5, ("present", "automatic", 80), floor=3, peak=8)
    chunks = [chunk(i, ("present", "automatic", 80)) for i in range(60)]
    chunks[5] = quiet
    chunks += [chunk(60 + i, ("present", "human", 100)) for i in range(60)]
    windows = budget.select(chunks, limit=1_000)
    assert {w.state for w in windows} == {("present", "automatic", 80), ("present", "human", 100)}
    assert next(w for w in windows if w.start == quiet.start).scale == 1


def test_an_empty_day_has_no_chunks():
    assert budget.chunks_of(day_of(end=START + 60), NO_LABELS, [0]) == []


def test_non_integer_readings_and_their_missing_values_are_read_too():
    day = day_of(
        dayfile.Change(START, {0: 10.5}),
        dayfile.Change(START + 100, {0: None}),
        dayfile.Change(START + 400, {0: 2.25}),
        end=START + 500,
    )
    (only,) = budget.chunks_of(day, NO_LABELS, [0])
    assert (only.peak, only.floor, only.seconds) == (10.5, 2.25, 200)


def test_unlabelled_time_keeps_only_its_newest_chunk_whole():
    chunks = [chunk(i, None, peak=90 if i == 3 else 5) for i in range(50)]
    whole = {w.start for w in budget.select(chunks, limit=1_000) if w.scale == 1}
    assert whole == {chunks[-1].start}


def test_observed_seconds_do_not_lose_precision_to_epoch_sized_sums():
    records = [dayfile.Change(START + i * 0.01337, {0: i % 50}) for i in range(4000)]
    day = day_of(*records, end=START + 60)
    found = budget.chunks_of(day, NO_LABELS, [0])
    exact = [end - start for start, end in zip(day.times, day.ends, strict=True)]
    first = found[0]
    assert first.seconds == pytest.approx(sum(exact[: first.rows]), rel=0, abs=1e-9)
