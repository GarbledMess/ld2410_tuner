"""Synthetic unload recovery keeps buffered readings on disk."""

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import test_tuner as harness
from tuner_under_test.history import dayfile
from tuner_under_test.history.daystore import bounds, date_of
from tuner_under_test.worker import flushing, transactions
from tuner_under_test.worker.supervisor import CleanupFailed, Closed, WorkerFailed

mod = harness.mod


class ShutdownHistoryTests(unittest.IsolatedAsyncioTestCase):
    setUp = harness.RuntimeTests.setUp
    asyncTearDown = harness.RuntimeTests.asyncTearDown

    def buffered(self):
        record = dayfile.Change(self.now - 1, {0: 37})
        self.runtime._days.append("a", record)
        self.hass.data = {mod.DOMAIN: self.runtime}
        return record

    def assert_saved(self, record):
        store = self.runtime._days
        date = date_of(self.now)
        records = dayfile.decode_log(store._path("a", date, ".log").read_bytes(), bounds(date)[0])
        self.assertIn(record, records)
        self.assertFalse(flushing._pending(store))

    async def unload(self):
        with patch.object(mod.frontend, "async_panel_exists", return_value=False, create=True):
            self.assertTrue(await asyncio.wait_for(mod.async_unload_entry(self.hass, None), 3))

    async def test_final_save_failure_recovers_buffered_readings_on_disk(self):
        record = self.buffered()
        with (
            patch.object(transactions, "final_save", side_effect=OSError("synthetic failure")),
            self.assertLogs(mod.__name__, level="WARNING") as logs,
        ):
            await self.unload()
        self.assert_saved(record)
        self.assertIn("synthetic failure", "\n".join(logs.output))
        self.assertIn("direct", "\n".join(logs.output))

    async def test_hung_final_save_is_abandoned_and_readings_saved_directly(self):
        record = self.buffered()

        async def hang(runtime):
            await asyncio.Event().wait()

        with (
            patch.object(transactions, "final_save", hang),
            patch.object(mod, "FINAL_SAVE_TIMEOUT", 0.01),
            self.assertLogs(mod.__name__, level="WARNING") as logs,
        ):
            await self.unload()
        self.assert_saved(record)
        self.assertIn("TimeoutError", "\n".join(logs.output))

    async def test_unadmitted_flush_is_cancelled_and_pending_records_saved_on_unload(self):
        record = self.buffered()
        waiting = asyncio.Event()

        async def never_admit(*args, **kwargs):
            waiting.set()
            await asyncio.Event().wait()

        with patch.object(transactions, "_admit", never_admit):
            self.runtime._flush_task = asyncio.create_task(flushing.flush(self.runtime))
            await asyncio.wait_for(waiting.wait(), 3)
            with patch.object(transactions, "final_save", new=AsyncMock()):
                await self.unload()
        self.assertTrue(self.runtime._flush_task.cancelled())
        self.assert_saved(record)

    async def test_failed_direct_flush_is_reported_without_blocking_unload(self):
        self.buffered()
        with (
            patch.object(transactions, "final_save", new=AsyncMock()),
            patch.object(self.runtime._days, "flush", side_effect=OSError("disk unavailable")),
            self.assertLogs(mod.__name__, level="WARNING") as logs,
        ):
            await self.unload()
        self.assertIn("Direct recorded history save failed", "\n".join(logs.output))
        self.assertIn("disk unavailable", "\n".join(logs.output))

    async def test_direct_flush_timeout_is_reported_and_unload_completes(self):
        self.buffered()

        async def blocked_executor(*args):
            await asyncio.Event().wait()

        with (
            patch.object(transactions, "final_save", new=AsyncMock()),
            patch.object(self.hass, "async_add_executor_job", blocked_executor),
            patch.object(mod, "FINAL_HISTORY_SAVE_TIMEOUT", 0),
            self.assertLogs(mod.__name__, level="WARNING") as logs,
        ):
            await self.unload()
        self.assertIn("Direct recorded history save failed", "\n".join(logs.output))
        self.assertIn("TimeoutError", "\n".join(logs.output))

    async def test_successful_worker_save_needs_no_direct_flush(self):
        record = self.buffered()
        store = self.runtime._days
        with patch.object(store, "flush", wraps=store.flush) as direct:
            await self.unload()
        direct.assert_not_called()
        self.assert_saved(record)

    async def test_worker_cleanup_failure_preserves_history_without_replacement_worker(self):
        record = self.buffered()
        with (
            patch.object(
                mod.bridge, "stop", side_effect=CleanupFailed("synthetic cleanup failure")
            ),
            patch.object(transactions, "final_save", new=AsyncMock()) as final_save,
            self.assertLogs(mod.__name__, level="WARNING") as logs,
        ):
            await self.unload()
        self.assert_saved(record)
        final_save.assert_not_awaited()
        self.assertIn("synthetic cleanup failure", "\n".join(logs.output))
        self.assertIn("direct", "\n".join(logs.output))

    def changed_setting(self):
        self.runtime.data["devices"]["a"] = {"recording_enabled": True}
        self.runtime.store.async_save.reset_mock()

    def assert_setting_saved(self):
        saved = self.runtime.store.async_save.await_args.args[0]
        self.assertTrue(saved["devices"]["a"]["recording_enabled"])
        self.assertIsNot(saved, self.runtime.data)

    async def test_worker_cleanup_failure_still_saves_labels_and_settings(self):
        self.buffered()
        self.changed_setting()
        with (
            patch.object(mod.bridge, "stop", side_effect=CleanupFailed("synthetic")),
            self.assertLogs(mod.__name__, level="WARNING"),
        ):
            await self.unload()
        self.assert_setting_saved()

    async def test_failed_final_save_still_saves_labels_and_settings(self):
        self.buffered()
        self.changed_setting()
        with (
            patch.object(transactions, "final_save", side_effect=OSError("synthetic")),
            self.assertLogs(mod.__name__, level="WARNING"),
        ):
            await self.unload()
        self.assert_setting_saved()

    async def test_hung_final_save_still_saves_labels_and_settings(self):
        self.buffered()
        self.changed_setting()

        async def hang(runtime):
            await asyncio.Event().wait()

        with (
            patch.object(transactions, "final_save", hang),
            patch.object(mod, "FINAL_SAVE_TIMEOUT", 0.01),
            self.assertLogs(mod.__name__, level="WARNING"),
        ):
            await self.unload()
        self.assert_setting_saved()

    async def test_a_failing_direct_settings_save_does_not_stop_the_history_save(self):
        record = self.buffered()
        self.changed_setting()
        self.runtime.store.async_save.side_effect = OSError("store unavailable")
        with (
            patch.object(transactions, "final_save", side_effect=OSError("synthetic")),
            self.assertLogs(mod.__name__, level="WARNING") as logs,
        ):
            await self.unload()
        self.assert_saved(record)
        self.assertIn("store unavailable", "\n".join(logs.output))

    async def test_successful_final_save_does_not_write_settings_twice(self):
        self.buffered()
        self.changed_setting()
        with patch.object(transactions, "final_save", new=AsyncMock()):
            await self.unload()
        self.runtime.store.async_save.assert_not_awaited()

    async def delayed_save_failing_with(self, error):
        self.changed_setting()
        with (
            patch.object(self.runtime, "async_save", side_effect=error),
            patch("tuner_under_test.runtime.coordinator.STORE_DELAY", 0),
            self.assertLogs(mod.__name__, level="WARNING") as logs,
        ):
            self.runtime._save_dirty = True
            await self.runtime._delayed_save()
        return "\n".join(logs.output)

    async def test_labels_and_settings_are_saved_when_the_worker_exceeds_its_budget(self):
        output = await self.delayed_save_failing_with(WorkerFailed(1, b"CpuBudgetExceeded"))
        self.assert_setting_saved()
        self.assertIn("CpuBudgetExceeded", output)

    async def test_labels_and_settings_are_saved_while_the_worker_is_quarantined(self):
        await self.delayed_save_failing_with(Closed("Worker supervisor is closed"))
        self.assert_setting_saved()

    async def test_labels_and_settings_are_saved_when_maintenance_is_refused(self):
        await self.delayed_save_failing_with(ValueError("Storage maintenance refused"))
        self.assert_setting_saved()

    async def test_a_failing_direct_save_is_reported_and_ends_the_delayed_save(self):
        self.runtime.store.async_save.side_effect = OSError("store unavailable")
        output = await self.delayed_save_failing_with(WorkerFailed(1, b"synthetic"))
        self.assertIn("store unavailable", output)
