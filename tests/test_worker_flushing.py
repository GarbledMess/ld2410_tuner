"""Flush transactions keep live buffers until a guarded publication acknowledges them."""

import shutil
import time
import unittest
from datetime import date
from unittest.mock import patch

import pytest
import test_tuner  # noqa: F401
from tuner_under_test.history import dayfile
from tuner_under_test.history.daystore import DayStore, bounds, date_of


def transaction(store, directory, now):
    from tuner_under_test.worker import flushing

    directory.mkdir()
    shutil.copytree(store.root, directory / "history", dirs_exist_ok=True)
    request, ownership = flushing.capture(store, directory, now)
    result = flushing.execute(
        {
            "root": str(directory / "history"),
            "directory": str(directory),
            "now": now,
            "flush": request,
        }
    )
    return result, ownership


def test_flush_acknowledges_only_captured_prefix_and_never_duplicates(tmp_path):
    from tuner_under_test.worker import flushing

    store = DayStore(tmp_path / "source")
    store.root.mkdir()
    start = bounds(date(2026, 10, 20))[0]
    first = dayfile.Change(start + 1, {0: 20})
    second = dayfile.Change(start + 2, {0: 30})
    store.append("a", first)
    result, ownership = transaction(store, tmp_path / "one", start + 3)
    assert store._live["a"].buffer == [first]
    store.append("a", second)
    flushing.publish(store, result, ownership, tmp_path / "one")
    assert store._live["a"].buffer == [second]
    result, ownership = transaction(store, tmp_path / "two", start + 4)
    flushing.publish(store, result, ownership, tmp_path / "two")
    assert store._live["a"].buffer == []
    raw = store._path("a", date(2026, 10, 20), ".log").read_bytes()
    assert dayfile.decode_log(raw, start) == [first, second]


def test_clear_after_capture_cannot_resurrect_history(tmp_path):
    from tuner_under_test.worker import flushing

    store = DayStore(tmp_path / "source")
    store.root.mkdir()
    start = bounds(date(2026, 10, 20))[0]
    store.append("a", dayfile.Change(start + 1, {0: 20}))
    result, ownership = transaction(store, tmp_path / "job", start + 3)
    store.remove_device("a")
    flushing.publish(store, result, ownership, tmp_path / "job")
    assert not store.folder("a").exists()


def test_failed_worker_leaves_original_buffers_and_closing_days(tmp_path):
    from tuner_under_test.worker import flushing

    store = DayStore(tmp_path / "source")
    store.root.mkdir()
    start = bounds(date(2026, 10, 19))[0]
    record = dayfile.Change(start + 1, {0: 20})
    store.append("a", record)
    directory = tmp_path / "job"
    directory.mkdir()
    snapshot, ownership = flushing.capture(store, directory, start + 86402)
    assert store._closing["a"][date(2026, 10, 19)].buffer == [record]
    with pytest.raises((ValueError, OSError)):
        flushing.execute(
            {
                "root": str(directory / "absent"),
                "directory": str(directory),
                "now": start + 86402,
                "flush": {**snapshot, "live": [{"bad": True}]},
            }
        )
    assert store._closing["a"][date(2026, 10, 19)].buffer == [record]
    assert ownership


def test_finished_day_and_late_guess_publish_losslessly(tmp_path):
    from tuner_under_test.worker import flushing

    store = DayStore(tmp_path / "source")
    store.root.mkdir()
    start = bounds(date(2026, 10, 19))[0]
    store.append("a", dayfile.Change(start + 1, {0: 20}))
    store.append("a", dayfile.Change(start + 86401, {0: 30}))
    store.append("a", dayfile.GuessSpan(start + 86402, start + 3, start + 7, "present", 80, 1))
    result, ownership = transaction(store, tmp_path / "job", start + 86403)
    flushing.publish(store, result, ownership, tmp_path / "job")
    assert not store._closing.get("a")
    assert not store._late.get("a")
    finished = store.day("a", date(2026, 10, 19))
    assert finished.vector(0)[0] == 20
    assert finished.guesses == [(start + 3, start + 7, "present", 80, 1)]


def test_failed_publication_rolls_back_before_retry(tmp_path):
    from tuner_under_test.worker import flushing

    store = DayStore(tmp_path / "source")
    store.root.mkdir()
    start = bounds(date(2026, 10, 20))[0]
    for key in ("a", "b"):
        store.append(key, dayfile.Change(start + 1, {0: 20}))
    result, ownership = transaction(store, tmp_path / "one", start + 3)
    original = flushing._publish_file
    calls = []

    def fail_second(*args):
        calls.append(True)
        if len(calls) == 2:
            raise OSError("disk full")
        return original(*args)

    with patch.object(flushing, "_publish_file", side_effect=fail_second):
        with pytest.raises(OSError, match="disk full"):
            flushing.publish(store, result, ownership, tmp_path / "one")
    assert not store._path("a", date(2026, 10, 20), ".log").exists()
    assert len(store._live["a"].buffer) == 1
    result, ownership = transaction(store, tmp_path / "two", start + 4)
    flushing.publish(store, result, ownership, tmp_path / "two")
    for key in ("a", "b"):
        raw = store._path(key, date(2026, 10, 20), ".log").read_bytes()
        assert len(dayfile.decode_log(raw, start)) == 1


def test_child_cannot_publish_an_uncaptured_path(tmp_path):
    from tuner_under_test.worker import flushing

    store = DayStore(tmp_path / "source")
    store.root.mkdir()
    start = bounds(date(2026, 10, 20))[0]
    store.append("a", dayfile.Change(start + 1, {0: 20}))
    result, ownership = transaction(store, tmp_path / "job", start + 3)
    result["effects"].append(["a", "2026-10-19", ".log", False])
    with pytest.raises(ValueError, match="manifest"):
        flushing.publish(store, result, ownership, tmp_path / "job")
    assert len(store._live["a"].buffer) == 1


def test_current_day_columns_are_not_copied_for_journal_flush(tmp_path):
    from tuner_under_test.worker import flushing

    store = DayStore(tmp_path / "source")
    start = bounds(date(2026, 10, 20))[0]
    for offset in range(20000):
        store.append("a", dayfile.Change(start + offset, dict.fromkeys(range(18), offset % 100)))
    metadata, ownership = flushing.capture(store, tmp_path, start + 20001)
    assert metadata["live"][0]["rows"] == 0
    assert 0 < ownership["counts"][0] < 20000  # Codec node allowance bounds this piece.
    assert len(store._live["a"].builder.times) == 20000


def test_closing_raw_byte_budget_is_checked_before_copy(tmp_path):
    from tuner_under_test.worker import flushing, startup

    store = DayStore(tmp_path / "source")
    start = bounds(date(2026, 10, 19))[0]
    store.append("a", dayfile.Change(start, {0: 20}))
    store.append("a", dayfile.Change(start + 86400, {0: 30}))
    with patch.object(startup, "MAX_BYTES", 1), patch.object(flushing.copy, "deepcopy") as copied:
        with pytest.raises(ValueError, match="byte"):
            flushing.capture(store, tmp_path, start + 86401)
    copied.assert_not_called()


@pytest.mark.parametrize(
    "record",
    [
        dayfile.End(10),
        dayfile.Guess(10, "present", 80, 1),
        dayfile.GuessSpan(10, 1, 2, "present", 80, 1),
    ],
)
def test_flush_record_types_round_trip(record):
    from tuner_under_test.worker import flushing

    assert flushing._restore(flushing._record(record)) == record


def test_unknown_records_fail_closed():
    from tuner_under_test.worker import flushing

    with pytest.raises(ValueError, match="record"):
        flushing._record(object())
    with pytest.raises(ValueError, match="record"):
        flushing._restore([99, 0])


def test_pending_record_budget_precedes_copies(tmp_path):
    from tuner_under_test.worker import flushing

    store = DayStore(tmp_path)
    now = time.time()
    store.append("a", dayfile.Change(now, {0: 20}))
    with (
        patch.object(flushing, "MAX_RECORDS", 0),
        patch.object(flushing.copy, "deepcopy") as copied,
    ):
        with pytest.raises(ValueError, match="pending-record"):
            flushing.capture(store, tmp_path, now)
    copied.assert_not_called()


def test_rollover_capture_allocates_only_one_window(tmp_path):
    from tuner_under_test.worker import flushing

    store = DayStore(tmp_path)
    now = time.time()
    store.append("a", dayfile.Change(now - 129 * 86400, {0: 20}))
    with patch.object(flushing, "ROLLOVER_DAYS", 2, create=True):
        flushing.capture(store, tmp_path, now)
    assert len(store._closing["a"]) == 2
    assert store._live["a"].date == date_of(now - 127 * 86400)


def test_corrupted_record_handoff_count_is_refused(tmp_path):
    from tuner_under_test.worker import flushing

    store = DayStore(tmp_path / "source")
    store.root.mkdir()
    now = time.time()
    store.append("a", dayfile.Change(now, {0: 20}))
    directory = tmp_path / "job"
    directory.mkdir()
    metadata, _ = flushing.capture(store, directory, now)
    (directory / "flush-records.json").write_text('{"records":[],"late":{}}')
    with pytest.raises(ValueError, match="count mismatch"):
        flushing.execute(
            {
                "flush": metadata,
                "root": str(directory / "history"),
                "directory": str(directory),
                "now": now,
            }
        )


class ProductionFlushTests(unittest.IsolatedAsyncioTestCase):
    setUp = test_tuner.RuntimeTests.setUp
    asyncTearDown = test_tuner.RuntimeTests.asyncTearDown

    async def test_flush_route_uses_transaction_and_preserves_concurrent_append(self):
        from tuner_under_test.worker import flushing

        now = time.time()
        store = self.runtime._days
        first = dayfile.Change(now, {0: 20})
        second = dayfile.Change(now + 1, {0: 30})
        store.append("a", first)

        async def concurrent(request):
            self.assertEqual(request["operation"], "flush")
            store.append("a", second)

        self.worker_transport.before = concurrent
        await flushing.flush(self.runtime)
        self.assertEqual(store._live["a"].buffer, [second])
        self.worker_transport.before = None
        await flushing.flush(self.runtime, locked=True)
        self.assertEqual(store._live["a"].buffer, [])

    async def test_flush_drains_finite_target_across_worker_turns(self):
        from tuner_under_test.worker import flushing

        now = time.time()
        store = self.runtime._days
        initial = [dayfile.Change(now + n, {0: 20 + n}) for n in range(5)]
        for record in initial:
            store.append("a", record)
        appended = []

        async def concurrent(request):
            record = dayfile.Change(now + 10 + len(appended), {0: 50 + len(appended)})
            appended.append(record)
            store.append("a", record)

        self.worker_transport.before = concurrent
        with patch.object(flushing, "MAX_RECORDS", 2):
            await flushing.flush(self.runtime)
        self.assertEqual(len(self.worker_transport.requests), 3)
        self.assertEqual(store._live["a"].buffer, appended)
        raw = store._path("a", date_of(now), ".log").read_bytes()
        self.assertEqual(dayfile.decode_log(raw, bounds(date_of(now))[0]), initial)
        self.worker_transport.before = None

    async def test_rollover_drains_to_captured_today_in_bounded_turns(self):
        from tuner_under_test.worker import flushing

        now = time.time()
        store = self.runtime._days
        store.append("a", dayfile.Change(now - 5 * 86400, {0: 20}))
        closing_sizes = []

        async def inspect(request):
            closing_sizes.append(sum(len(days) for days in store._closing.values()))

        self.worker_transport.before = inspect
        with patch.object(flushing, "ROLLOVER_DAYS", 2, create=True):
            await flushing.flush(self.runtime)
        self.assertEqual(store.live_date("a"), date_of(now))
        self.assertFalse(any(store._closing.values()))
        self.assertFalse(store._live["a"].buffer)
        self.assertEqual(len(closing_sizes), 3)
        self.assertLessEqual(max(closing_sizes), 2)
        self.assertEqual(len(list(store.folder("a").glob("*.day"))), 5)
        self.worker_transport.before = None

    async def test_device_count_is_batched_instead_of_refused(self):
        from tuner_under_test.worker import flushing

        now = time.time()
        store = self.runtime._days
        for number in range(129):
            store.append(str(number), dayfile.Change(now, {0: 20}))
        await flushing.flush(self.runtime)
        self.assertEqual(len(self.worker_transport.requests), 2)
        self.assertTrue(all(not live.buffer for live in store._live.values()))

    async def test_empty_runtime_needs_no_worker(self):
        from tuner_under_test.worker import flushing

        self.runtime._days = None
        self.assertEqual(await flushing.flush(self.runtime), 0)
        self.assertEqual(self.worker_transport.requests, [])

    async def test_store_without_pending_writes_needs_no_worker(self):
        from tuner_under_test.worker import flushing

        self.assertEqual(await flushing.flush(self.runtime), 0)
        self.assertEqual(self.worker_transport.requests, [])
        now = time.time()
        self.runtime._days.append("a", dayfile.Change(now, {0: 20}))
        await flushing.flush(self.runtime)
        self.assertEqual(len(self.worker_transport.requests), 1)
        self.assertEqual(await flushing.flush(self.runtime), 0)
        self.assertEqual(len(self.worker_transport.requests), 1)

    async def test_no_op_guard_still_rolls_a_durable_previous_day(self):
        from tuner_under_test.worker import flushing

        yesterday = time.time() - 86400
        store = self.runtime._days
        store.append("a", dayfile.Change(yesterday, {0: 20}))
        store.flush(yesterday)
        self.assertEqual(store._live["a"].buffer, [])
        await flushing.flush(self.runtime)
        self.assertEqual(len(self.worker_transport.requests), 1)
        self.assertTrue(store._path("a", date_of(yesterday), ".day").exists())

    async def test_clear_during_publication_is_drained_before_next_snapshot(self):
        from tuner_under_test.worker import flushing

        now = time.time()
        store = self.runtime._days
        store.append("a", dayfile.Change(now, {0: 20}))
        replacement = dayfile.Change(now + 1, {0: 30})
        original = flushing._publish_file

        def clear_during_write(*args):
            store.forget_device("a")
            store.append("a", replacement)
            return original(*args)

        with patch.object(flushing, "_publish_file", side_effect=clear_during_write):
            await flushing.flush(self.runtime)
        await flushing.flush(self.runtime)
        date = store.live_date("a")
        raw = store._path("a", date, ".log").read_bytes()
        self.assertEqual(dayfile.decode_log(raw, bounds(date)[0]), [replacement])


def test_bounded_flush_drains_closing_and_late_prefixes(tmp_path):
    from tuner_under_test.worker import flushing

    store = DayStore(tmp_path / "source")
    store.root.mkdir()
    start = bounds(date(2026, 10, 19))[0]
    records = [dayfile.Change(start + offset, {0: offset}) for offset in range(1, 6)]
    for record in records:
        store.append("a", record)
    store.append("a", dayfile.Change(start + 86401, {0: 30}))
    spans = [
        dayfile.GuessSpan(start + 86402 + n, start + n * 2, start + n * 2 + 1, "present", 80, 1)
        for n in range(3)
    ]
    for span in spans:
        store.append("a", span)
    with patch.object(flushing, "MAX_RECORDS", 2):
        for index in range(10):
            result, ownership = transaction(store, tmp_path / str(index), start + 86410)
            assert (
                sum(ownership["counts"])
                + sum(
                    len(values) for dates in ownership["late"].values() for values in dates.values()
                )
                <= 2
            )
            flushing.publish(store, result, ownership, tmp_path / str(index))
            if not flushing._pending(store):
                break
        assert not store._closing.get("a")
        assert not store._late.get("a")
        assert not store._live["a"].buffer
    finished = store.day("a", date(2026, 10, 19))
    assert list(finished.times) == [record.time for record in records]
    assert finished.guesses == [(span.start, span.end, "present", 80, 1) for span in spans]


def test_record_bytes_choose_prefix_before_copy(tmp_path):
    from tuner_under_test.worker import flushing

    store = DayStore(tmp_path / "source")
    store.root.mkdir()
    now = time.time()
    first = dayfile.Guess(now, "present", 80, 1)
    second = dayfile.Guess(now + 1, "absent", 70, 2)
    store.append("a", first)
    store.append("a", second)
    # Leave room for one record after the reserved JSON container overhead.
    import json

    size = len(json.dumps(flushing._record(first), separators=(",", ":")).encode()) + 1
    with patch.object(flushing, "MAX_MESSAGE_BYTES", 65536 + size):
        result, ownership = transaction(store, tmp_path / "job", now + 2)
        assert ownership["counts"] == [1]
        assert store._live["a"].buffer == [first, second]
        flushing.publish(store, result, ownership, tmp_path / "job")
    assert store._live["a"].buffer == [second]


def test_closing_days_share_resident_heap_budget_across_batches(tmp_path):
    from tuner_under_test.worker import flushing, startup

    store = DayStore(tmp_path / "source")
    store.root.mkdir()
    start = bounds(date(2026, 10, 19))[0]
    for key in ("a", "b"):
        store.append(key, dayfile.Change(start + 1, {0: 20}))
        store.append(key, dayfile.Change(start + 86401, {0: 30}))
    with patch.object(startup, "MAX_DECODED_BYTES", 4 * 4096):
        result, ownership = transaction(store, tmp_path / "one", start + 86403)
        assert sum(done for _, _, done in ownership["items"]) == 1
        flushing.publish(store, result, ownership, tmp_path / "one")
        assert sum(len(days) for days in store._closing.values()) == 1
        result, ownership = transaction(store, tmp_path / "two", start + 86403)
        flushing.publish(store, result, ownership, tmp_path / "two")
    assert not any(store._closing.values())
    for key in ("a", "b"):
        assert store.day(key, date(2026, 10, 19)).vector(0)[0] == 20
