"""Plain journal appends are written in-process; only rewrites of history start a worker.

A flush ran every few seconds while recording. Sending a few hundred appended bytes through a
limited child copied each radar's whole growing day log twice and started a Python process;
appending in the executor keeps the same guarantees: records stay buffered until written, a
failed append leaves the log as it was, and a clear is never resurrected.
"""

import time
import unittest
from unittest.mock import patch

import test_tuner  # noqa: F401
from tuner_under_test.history import dayfile
from tuner_under_test.history.daystore import DayStore, bounds, date_of


def _logged(store, key, now):
    raw = store._path(key, date_of(now), ".log").read_bytes()
    return dayfile.decode_log(raw, bounds(date_of(now))[0])


class AppendRouteTests(unittest.IsolatedAsyncioTestCase):
    setUp = test_tuner.RuntimeTests.setUp
    asyncTearDown = test_tuner.RuntimeTests.asyncTearDown

    async def test_appends_alone_start_no_worker_and_write_every_record_once(self):
        from tuner_under_test.worker import flushing

        now = time.time()
        store = self.runtime._days
        first = [dayfile.Change(now + n, {0: 20 + n}) for n in range(3)]
        for record in first:
            store.append("a", record)
        store.append("b", dayfile.Change(now, {1: 40}))
        written = await flushing.flush(self.runtime)
        self.assertGreater(written, 0)
        self.assertEqual(self.worker_transport.requests, [])
        self.assertEqual(_logged(store, "a", now), first)
        self.assertEqual(_logged(store, "b", now), [dayfile.Change(now, {1: 40})])
        self.assertFalse(store._live["a"].buffer or store._live["b"].buffer)
        later = dayfile.Change(now + 5, {0: 9})
        store.append("a", later)
        await flushing.flush(self.runtime)
        self.assertEqual(_logged(store, "a", now), [*first, later])
        self.assertEqual(self.worker_transport.requests, [])

    async def test_a_record_appended_during_the_write_stays_buffered(self):
        from tuner_under_test.worker import flushing

        now = time.time()
        store = self.runtime._days
        first = dayfile.Change(now, {0: 20})
        second = dayfile.Change(now + 1, {0: 30})
        store.append("a", first)
        encode = dayfile.encode_records

        def concurrent(records, written):
            store.append("a", second)
            return encode(records, written)

        with patch.object(dayfile, "encode_records", side_effect=concurrent):
            await flushing.flush(self.runtime)
        self.assertEqual(store._live["a"].buffer, [second])
        self.assertEqual(_logged(store, "a", now), [first])
        await flushing.flush(self.runtime)
        self.assertEqual(_logged(store, "a", now), [first, second])

    async def test_a_failed_append_leaves_the_log_whole_and_the_records_buffered(self):
        from tuner_under_test.history import daystore
        from tuner_under_test.worker import flushing

        now = time.time()
        store = self.runtime._days
        first = dayfile.Change(now, {0: 20})
        store.append("a", first)
        await flushing.flush(self.runtime)
        second = dayfile.Change(now + 1, {0: 30})
        store.append("a", second)

        def torn(log, data):
            log.write(data[: len(data) // 2])
            raise OSError("disk full")

        with patch.object(daystore, "_write_bytes", side_effect=torn):
            with self.assertRaises(OSError):
                await flushing.flush(self.runtime)
        self.assertEqual(_logged(store, "a", now), [first])
        self.assertEqual(store._live["a"].buffer, [second])
        await flushing.flush(self.runtime)
        self.assertEqual(_logged(store, "a", now), [first, second])

    async def test_a_previous_day_or_a_clear_still_goes_through_the_worker(self):
        from tuner_under_test.worker import flushing

        now = time.time()
        store = self.runtime._days
        store.append("a", dayfile.Change(now - 86400, {0: 20}))
        await flushing.flush(self.runtime)
        self.assertEqual(len(self.worker_transport.requests), 1)
        store.append("b", dayfile.Change(now, {0: 20}))
        store.forget_device("b")
        store.append("b", dayfile.Change(now + 1, {0: 30}))
        await flushing.flush(self.runtime)
        self.assertEqual(len(self.worker_transport.requests), 2)
        self.assertEqual(_logged(store, "b", now), [dayfile.Change(now + 1, {0: 30})])

    async def test_a_closed_worker_still_lets_a_day_rewrite_save_directly(self):
        from tuner_under_test.worker import flushing
        from tuner_under_test.worker.supervisor import Closed

        now = time.time()
        store = self.runtime._days
        store.append("a", dayfile.Change(now - 86400, {0: 20}))
        today = dayfile.Change(now, {0: 30})
        store.append("b", today)
        with patch.object(flushing, "_drain", side_effect=Closed("Worker supervisor is closed")):
            await flushing.flush(self.runtime)
        self.assertTrue(store._path("a", date_of(now - 86400), ".day").exists())
        self.assertEqual(_logged(store, "b", now), [today])
        self.runtime._worker_closed = True  # Unloading: the closed worker is final.
        store.append("c", dayfile.Change(now - 86400, {0: 20}))
        with patch.object(flushing, "_drain", side_effect=Closed("Worker supervisor is closed")):
            with self.assertRaises(Closed):
                await flushing.flush(self.runtime)


def test_append_of_a_cleared_generation_writes_nothing(tmp_path):
    from tuner_under_test.worker import flushing

    store = DayStore(tmp_path)
    now = time.time()
    store.append("a", dayfile.Change(now, {0: 20}))
    live = store._live["a"]
    store.forget_device("a")
    store._clear_pending.clear()
    assert store.append_log("a", live, 1) == 0
    assert not store._path("a", date_of(now), ".log").exists()
    assert flushing.append_logs(store, now) == 0


def test_an_empty_prefix_writes_nothing(tmp_path):
    store = DayStore(tmp_path)
    now = time.time()
    store.append("a", dayfile.Change(now, {0: 20}))
    assert store.append_log("a", store._live["a"], 0) == 0
    assert not store._path("a", date_of(now), ".log").exists()


def test_a_clear_during_the_append_never_acknowledges_the_new_recording(tmp_path):
    from tuner_under_test.history import daystore
    from tuner_under_test.worker import flushing

    store = DayStore(tmp_path)
    now = time.time()
    store.append("a", dayfile.Change(now, {0: 20}))
    write = daystore._write_bytes
    after = dayfile.Change(now + 1, {0: 30})

    def clear_while_writing(log, data):
        write(log, data)
        store.forget_device("a")
        store.append("a", after)

    with patch.object(daystore, "_write_bytes", side_effect=clear_while_writing):
        flushing.append_logs(store, now)
    assert store._live["a"].buffer == [after]
    store.flush(now)  # The clear drains first; only the new recording remains.
    assert _logged(store, "a", now) == [after]
