"""Physical-day histograms equal held-span replay without walking rows on each edit."""

import random
from array import array
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import test_tuner as harness
from tuner_under_test.history import dayfile, label_counts, labels, spans


def synthetic_day(seed=27):
    rng = random.Random(seed)
    builder = dayfile.DayBuilder(0, 86400)
    for i in range(150):
        if i % 31 == 30:
            builder.add(dayfile.End(i * 3))
        values = {gate: rng.choice((None, 0, 10, 10.1, 10.9, 100)) for gate in range(18)}
        builder.add(dayfile.Change(i * 3 + 0.125, values))
    builder.add(dayfile.End(451))
    return builder.day()


def test_summary_cache_size_includes_the_legacy_day_it_keeps_alive():
    from tuner_under_test.history import summaries

    rows = 1000
    day = dayfile.Day(
        0,
        rows,
        array("d", range(rows)),
        array("d", range(1, rows + 1)),
        [array("f", [10.25]) * rows for _ in range(18)],
        [],
    )
    summary = summaries.Summary(day)
    physical_bytes = rows * (16 + 18 * 4)
    prefix_bytes = (rows + 1) * 8 * 2
    assert summary.nbytes >= physical_bytes + prefix_bytes


def test_day_summary_histograms_equal_full_replay_for_arbitrary_windows():
    from tuner_under_test.history import summaries

    day = synthetic_day()
    summary = summaries.of_day(day)
    rng = random.Random(28)
    for _ in range(30):
        low, high = sorted((rng.uniform(-10, 470), rng.uniform(-10, 470)))
        raw = label_counts.empty()

        def read(t):
            return "present"

        read.edges = []
        label_counts._count_native(raw, spans.day_spans(day, low, high), read, low, 1)
        found = summary.histograms(low, high)
        for gate, key in enumerate(harness.constants.HISTORY_KEYS):
            assert found[gate] == pytest.approx(raw[key]["present"], abs=1e-3)


def test_warmed_label_edit_uses_summary_instead_of_reconstructing_physical_rows():
    runtime = SimpleNamespace(
        data={"devices": {"a": {"history_labels": [{"start": 0, "end": 600, "state": "present"}]}}},
        _label_counts={},
    )
    harness.recorded(runtime, "a", [(i, [i % 80] * 18) for i in range(600)], 600)
    runtime._days.flush(now=86401)
    with patch.object(label_counts.time, "time", return_value=700):
        label_counts.current(runtime, runtime.data["devices"]["a"])
        runtime.data["devices"]["a"]["history_labels"][0]["state"] = "not_present"
        with patch.object(spans, "day_spans", side_effect=AssertionError("replayed cached day")):
            found = label_counts.current(runtime, runtime.data["devices"]["a"])
    assert sum(found["g0_move"]["not_present"]) == 100
    assert sum(found["g0_move"]["present"]) == 0


def test_summary_handles_empty_and_byte_columns_without_inventing_observations():
    from tuner_under_test.history import summaries

    empty = dayfile.Day(0, 86400, array("d"), array("d"), [b""] * 18, [])
    assert summaries.of_day(empty).histograms(0, 100) == [[0.0] * 101 for _ in range(18)]


def test_observed_seconds_uses_warmed_physical_summary_without_span_replay():
    from tuner_under_test.history import summaries

    runtime = SimpleNamespace(data={"devices": {"a": {}}})
    harness.recorded(runtime, "a", [(100, [None] * 18), (110, [20] + [None] * 17)], 120)
    day = runtime._days.day("a", daystore_date(100), until=120)
    summaries.of_day(day)
    with (
        patch.object(spans, "events", side_effect=AssertionError("row replay")),
        patch.object(labels.time, "time", return_value=200),
    ):
        assert labels._observed_seconds(runtime, "a", 105, 115) == 5


def daystore_date(timestamp):
    from tuner_under_test.history.daystore import date_of

    return date_of(timestamp)


def test_live_summary_reuses_identical_snapshots_and_invalidates_on_new_readings():
    from tuner_under_test.history import summaries

    builder = dayfile.DayBuilder(0, 86400)
    builder.add(dayfile.Change(100, {0: 10}))
    first = summaries.of_day(builder.view(110))
    assert summaries.of_day(builder.view(110)) is first
    builder.add(dayfile.Change(105, {0: 30}))
    second = summaries.of_day(builder.view(110))
    assert second is not first
    assert first.histograms(100, 110)[0][10] == 10
    assert second.histograms(100, 110)[0][30] == 5


def test_coverage_ranges_equal_replay_including_unknown_rows_and_recording_gaps():
    from tuner_under_test.history import summaries

    day = synthetic_day()
    summary = summaries.of_day(day)
    for low, high in ((-10, 470), (10.5, 195.25), (90, 99), (0, 0), (500, 600)):
        assert list(summary.coverage_ranges(low, high)) == spans.coverage(
            spans.day_spans(day, low, high)
        )
    builder = dayfile.DayBuilder(0, 86400)
    builder.add(dayfile.Change(10, {0: 20}))
    builder.add(dayfile.Change(20, {0: None}))
    builder.add(dayfile.Change(30, {0: 30}))
    builder.add(dayfile.End(40))
    builder.add(dayfile.Change(50, {0: 40}))
    builder.add(dayfile.End(60))
    assert list(summaries.of_day(builder.day()).coverage_ranges(15, 55)) == [
        (15, 20),
        (30, 40),
        (50, 55),
    ]


def test_ensure_prepares_persistent_indexes_in_executor_for_restart(tmp_path):
    import asyncio

    from tuner_under_test.history import daystore, summaries
    from tuner_under_test.worker import supervisor
    from worker_transport import Transport

    transport = Transport()

    async def process(service, args):
        return await transport.process(args)

    device = {"history_labels": [{"start": 0, "end": 500, "state": "present"}]}
    runtime = SimpleNamespace(
        data={"devices": {"a": device}},
        _label_counts={},
        _days=daystore.DayStore(tmp_path / "history"),
        hass=SimpleNamespace(async_add_executor_job=asyncio.to_thread),
    )
    date = daystore.date_of(0)
    runtime._days.replace("a", date, synthetic_day())
    with (
        patch.object(label_counts.time, "time", return_value=700),
        patch.object(supervisor.Supervisor, "_process", process),
    ):
        asyncio.run(label_counts.ensure(runtime, "a"))
    expected = runtime._label_counts["a"].raw
    assert runtime._days._path("a", date, ".sum").exists()
    runtime._days = daystore.DayStore(runtime._days.root)
    asyncio.run(asyncio.to_thread(runtime._days.open, 700))
    runtime._label_counts.clear()
    with (
        patch.object(label_counts.time, "time", return_value=700),
        patch.object(summaries.Summary, "__init__", side_effect=AssertionError("rebuilt")),
        patch.object(supervisor.Supervisor, "_process", process),
    ):
        asyncio.run(label_counts.ensure(runtime, "a"))
    assert runtime._label_counts["a"].raw == expected


def test_storage_discards_auxiliary_index_before_trimming_physical_history(tmp_path):
    from tuner_under_test.history import budget, daystore, storage

    store = daystore.DayStore(tmp_path / "history")
    date = daystore.date_of(0)
    day = synthetic_day()
    store.replace("a", date, day)
    store.summary("a", date)
    data = {"devices": {"a": {"history_labels": []}}}
    target = store._path("a", date, ".day").stat().st_size + budget.storage_bytes(data) + 1
    prepared, changes, report, staged = storage._prepare_day_save(store, data, 700, False, target)
    store.discard(staged)
    assert not changes
    assert not store._path("a", date, ".sum").exists()
    assert list(store.day("a", date).times) == list(day.times)
    assert report["used_bytes"] <= target


def test_observed_summary_respects_snapshot_physical_and_attribution_boundaries():
    runtime = SimpleNamespace(data={"devices": {"a": {}}})
    harness.recorded(runtime, "a", [(100, [20] * 18)], 120)
    runtime._physical_history_start = 109
    runtime._attribution_until = 115
    runtime._history_until = 118
    with patch.object(labels.time, "time", return_value=200):
        assert labels._observed_seconds(runtime, "a", 100, 120) == 6


@pytest.mark.parametrize("gates", [[], [0], [3, 7], list(range(18))])
def test_selected_gate_coverage_equals_physical_replay(gates):
    from tuner_under_test.history import summaries

    day = synthetic_day()
    expected = []
    for cell in spans.day_spans(day, 10.5, 301.7):
        if any(cell.values[gate] is not None for gate in gates):
            if expected and expected[-1][1] == cell.start:
                expected[-1] = expected[-1][0], cell.end
            else:
                expected.append((cell.start, cell.end))
    assert list(summaries.of_day(day).valid_ranges(10.5, 301.7, gates)) == expected


def test_learning_bounds_use_native_columns_without_materialising_every_vector():
    from tuner_under_test.calibration import event_fitting

    runtime = SimpleNamespace(data={"devices": {"a": {}}})
    harness.recorded(runtime, "a", [(100, [10] * 18), (110, [20] * 18)], 120)
    with patch.object(dayfile.Day, "vector", side_effect=AssertionError("full replay")):
        assert event_fitting._bounds(runtime, "a", 105, 115) == (105, 115)
        assert event_fitting._bounds(runtime, "a", 0, 100) == (0, 0)
        assert event_fitting._bounds(runtime, "a", 115, 200) == (115, 120)
    runtime._physical_history_start = 108
    runtime._attribution_until = 113
    assert event_fitting._bounds(runtime, "a", 0, 200) == (108, 113)


def test_complete_label_horizon_proof_does_not_build_a_second_set_of_day_indexes():
    from tuner_under_test.history import summaries
    from tuner_under_test.history.label_lookup import index_reader

    runtime = SimpleNamespace(data={"devices": {"a": {}}})
    harness.recorded(runtime, "a", [(100, [10] * 18)], 120)
    with patch.object(summaries.Summary, "__init__", side_effect=AssertionError("premature index")):
        assert summaries.fully_labelled(runtime, "a", index_reader(([0], ["present"])), 0, 200)


def test_label_proof_ignores_unrecorded_days_before_retained_physical_days(tmp_path):
    from tuner_under_test.history import daystore, summaries
    from tuner_under_test.history.label_lookup import index_reader

    store = daystore.DayStore(tmp_path / "history")
    builder = dayfile.DayBuilder(86400, 172800)
    builder.add(dayfile.Change(86410, {0: 20}))
    builder.add(dayfile.End(86420))
    store.replace("a", daystore.date_of(86400), builder.day())
    runtime = SimpleNamespace(_days=store)
    own = index_reader(([86400, 172800], ["present", None]))
    with patch.object(
        summaries.Summary, "__init__", side_effect=AssertionError("redundant index pass")
    ):
        assert summaries.fully_labelled(runtime, "a", own, 0, 172800)


def test_locked_auxiliary_index_cannot_force_physical_history_trimming(tmp_path):
    from tuner_under_test.history import budget, daystore, storage, summary_cache

    store = daystore.DayStore(tmp_path / "history")
    builder = dayfile.DayBuilder(0, 86400)
    rng = random.Random(72)
    for index in range(5000):
        builder.add(dayfile.Change(index * 2, {gate: rng.randrange(101) for gate in range(18)}))
    builder.add(dayfile.End(10000))
    date = daystore.date_of(0)
    store.replace("a", date, builder.day())
    physical = store._path("a", date, ".day")
    original = physical.read_bytes()
    store._path("a", date, ".sum").write_bytes(b"optional-locked-cache" * 3000)
    data = {"devices": {"a": {"history_labels": []}}}
    limit = len(original) + budget.storage_bytes(data) + 1
    with (
        patch.object(summary_cache, "_remove", return_value=False),
        patch.object(budget, "prepare_days", side_effect=AssertionError("physical trim attempted")),
        pytest.raises(budget.BudgetExceeded, match="index"),
    ):
        storage._prepare_day_save(store, data, 86401, False, limit)
    assert physical.read_bytes() == original


def integer_day(seed):
    """Byte columns only: repeated values (long runs), missing readings and recording gaps."""
    rng = random.Random(seed)
    builder = dayfile.DayBuilder(0, 86400)
    for i in range(400):
        if i % 47 == 46:
            builder.add(dayfile.End(i * 2))
        values = {
            gate: rng.choice((None, 0, 5, 5, 5, 37, 100))
            for gate in rng.sample(range(18), rng.randint(1, 18))
        }
        builder.add(dayfile.Change(i * 2 + 0.5, values))
    builder.add(dayfile.End(801))
    return builder.day()


@pytest.mark.parametrize("seed", [3, 4, 5])
def test_byte_column_summaries_equal_replay(seed):
    from tuner_under_test.history import summaries

    day = integer_day(seed)
    assert all(isinstance(column, bytes) for column in day.columns)
    summary = summaries.of_day(day)
    rng = random.Random(seed + 100)

    def read(t):
        return "present"

    read.edges = []
    for _ in range(25):
        low, high = sorted((rng.uniform(-5, 810), rng.uniform(-5, 810)))
        raw = label_counts.empty()
        replay = list(spans.day_spans(day, low, high))
        label_counts._count_native(raw, replay, read, low, 1)
        found = summary.histograms(low, high)
        for gate, key in enumerate(harness.constants.HISTORY_KEYS):
            assert found[gate] == pytest.approx(raw[key]["present"], abs=1e-3)
        assert list(summary.coverage_ranges(low, high)) == spans.coverage(replay)
        assert summary.observed(low, high) == pytest.approx(
            sum(high - low for low, high in spans.coverage(replay)), abs=1e-3
        )


def test_coverage_joins_readings_that_meet_across_a_zero_length_missing_row():
    from tuner_under_test.history import summaries

    builder = dayfile.DayBuilder(0, 86400)
    builder.add(dayfile.Change(10, {0: 20}))
    builder.add(dayfile.Change(20, {0: None}))
    builder.add(dayfile.End(20))
    builder.add(dayfile.Change(20, {0: 30}))
    builder.add(dayfile.End(40))
    day = builder.day()
    expected = spans.coverage(list(spans.day_spans(day, 0, 50)))
    assert list(summaries.of_day(day).coverage_ranges(0, 50)) == expected == [(10, 40)]


def test_learning_bounds_read_only_the_outermost_observed_days(tmp_path):
    from tuner_under_test.history import daystore

    store = daystore.DayStore(tmp_path / "history")
    store.open(now=0)
    for i in range(5 * 24):
        store.append("a", dayfile.Change(i * 3600 + 5, {0: i % 9}))
    store.append("a", dayfile.End(5 * 86400 - 100))
    store.flush(now=5 * 86400 + 1)
    view = SimpleNamespace(_days=store, _history_until=5 * 86400)
    expected = spans.bounds(view, "a", 10, 5 * 86400)
    read = []
    real = store.day
    with patch.object(store, "day", side_effect=lambda *a, **k: read.append(a[1]) or real(*a, **k)):
        assert spans.bounds(view, "a", 10, 5 * 86400) == expected == (10, 5 * 86400 - 100)
    assert len(read) == 2
