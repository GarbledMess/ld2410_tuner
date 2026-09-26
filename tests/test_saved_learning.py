"""Saved-result selection and local-time overnight learning, using synthetic devices."""

import asyncio
import sys
import types
import unittest
from copy import deepcopy
from datetime import datetime
from unittest.mock import AsyncMock, Mock

import test_tuner as harness

results = sys.modules["tuner_under_test.calibration.results"]
schedule = sys.modules["tuner_under_test.runtime.schedule"]


class SavedLearningTests(unittest.IsolatedAsyncioTestCase):
    setUp = harness.RuntimeTests.setUp
    asyncTearDown = harness.RuntimeTests.asyncTearDown
    configuration = harness.RuntimeTests.configuration

    def saved(self, source="user", threshold=20):
        self.configuration()
        learned = deepcopy(self.device["last_learning"])
        learned.update(created_at=self.now, label_revision=0)
        for proposal in learned["proposals"].values():
            proposal["threshold"] = threshold
        return results.remember_learning(self.device, learned, source)

    async def apply_slot(self, slot):
        chosen = results.saved_results(self.device)[slot]
        _, current = self.runtime._threshold_configuration("a")
        return await self.runtime.apply("a", slot, chosen["id"], current)

    def test_migration_and_bounded_independent_slots(self):
        self.configuration()
        original = deepcopy(self.device["last_learning"])
        saved = results.saved_results(self.device)
        self.assertEqual(saved["user"]["proposals"], original["proposals"])
        first_id = saved["user"]["id"]
        for _ in range(10):
            results.remember_learning(self.device, original, "automatic")
        self.assertEqual(set(saved), {"user", "automatic"})
        self.assertEqual(saved["user"]["id"], first_id)
        self.assertEqual(self.device["last_learning"], original)

    async def test_apply_and_rollback_preserve_manual_and_automatic_results(self):
        user = self.saved()
        await self.apply_slot("user")
        saved = results.saved_results(self.device)
        self.assertEqual(saved["current"]["id"], user["id"])
        self.assertEqual(saved["previous"]["proposals"]["g0_move"]["threshold"], 10)
        auto = deepcopy(user)
        auto.pop("id")
        for proposal in auto["proposals"].values():
            proposal["threshold"] = 30
        auto = results.remember_learning(self.device, auto, "automatic")
        await self.apply_slot("automatic")
        self.assertEqual(saved["previous"]["id"], user["id"])
        self.assertEqual(saved["current"]["id"], auto["id"])
        self.device["label_revision"] = 5
        await self.apply_slot("previous")
        self.assertEqual(saved["current"]["id"], user["id"])
        self.assertEqual(saved["previous"]["id"], auto["id"])
        self.assertEqual(saved["automatic"]["id"], auto["id"])
        self.assertEqual(saved["user"]["id"], user["id"])
        previous = deepcopy(saved["previous"])
        await self.apply_slot("current")
        self.assertEqual(saved["previous"], previous)

    async def test_selected_result_replacement_and_device_changes_reject_before_writes(self):
        learned = self.saved()
        expected = learned["configuration"]
        for slot, result_id in (("unknown", learned["id"]), ("user", "replaced")):
            with self.assertRaises(ValueError):
                await self.runtime.apply("a", slot, result_id, expected)
        self.states["number.radar_g0_move_threshold"].state = "11"
        with self.assertRaisesRegex(ValueError, "configuration changed"):
            await self.runtime.apply("a", "user", learned["id"], expected)
        self.hass.services.async_call.assert_not_awaited()

    async def test_partial_apply_does_not_promote_a_result(self):
        self.saved()
        self.hass.services.async_call.side_effect = RuntimeError("offline")
        result = await self.apply_slot("user")
        self.assertTrue(result["skipped"])
        self.assertNotIn("current", results.saved_results(self.device))
        self.assertNotIn("previous", results.saved_results(self.device))

    async def test_explicit_learning_and_scheduled_learning_share_fit_not_slots(self):
        self.configuration()
        learned = deepcopy(self.device["last_learning"])
        learned.update(label_revision=0, created_at=self.now)
        self.runtime._fit_history = Mock(return_value=learned)
        self.runtime._history_view = lambda _: self.runtime
        user, auto = await asyncio.gather(
            self.runtime.async_learn("a"), self.runtime.async_learn("a", source="automatic")
        )
        self.runtime._fit_history.assert_called_once()
        self.assertEqual(user["source"], "user")
        self.assertEqual(auto["source"], "automatic")
        self.assertEqual(self.device["last_learning"]["id"], user["id"])
        self.assertNotEqual(user["id"], auto["id"])
        self.hass.services.async_call.assert_not_awaited()
        with self.assertRaises(ValueError):
            await self.runtime.async_learn("a", source="invalid")

    def test_clear_removes_saved_results_and_overnight_marker(self):
        self.saved()
        self.device["nightly_learning"] = {"status": "ok"}
        self.runtime.clear_samples("a")
        self.assertNotIn("learning_results", self.device)
        self.assertNotIn("nightly_learning", self.device)


class ScheduleTests(unittest.IsolatedAsyncioTestCase):
    setUp = harness.RuntimeTests.setUp
    asyncTearDown = harness.RuntimeTests.asyncTearDown

    def configure(self, at="03:00"):
        self.hass.config = types.SimpleNamespace(time_zone="Europe/London")
        self.runtime.configure_learning_schedule(True, at)
        self.runtime.async_learn = AsyncMock(return_value={"status": "ok", "id": "new"})

    async def tick(self, instant):
        await self.runtime.nightly_tick(datetime.fromisoformat(instant))
        if self.runtime._nightly_task:
            await self.runtime._nightly_task

    async def test_disabled_and_before_time_do_not_run_then_restart_does_not_repeat(self):
        await self.tick("2026-09-26T05:00:00+00:00")
        self.assertIsNone(self.runtime._nightly_task)
        self.configure()
        await self.tick("2026-09-26T01:59:00+00:00")
        self.runtime.async_learn.assert_not_awaited()
        await self.tick("2026-09-26T02:00:00+00:00")
        self.runtime.async_learn.assert_awaited_once_with("a", source="automatic")
        self.assertEqual(self.device["nightly_learning"]["status"], "ok")
        self.runtime.store.async_save.assert_awaited()
        self.runtime._nightly_task = None  # Same persisted data after restart.
        await self.tick("2026-09-26T09:00:00+00:00")
        self.assertEqual(self.runtime.async_learn.await_count, 1)
        await self.tick("2026-09-27T03:00:00+00:00")
        self.assertEqual(self.runtime.async_learn.await_count, 2)
        self.hass.services.async_call.assert_not_awaited()

    async def test_dst_repeated_hour_runs_once_and_skipped_time_runs_after_jump(self):
        self.configure("01:30")
        await self.tick("2026-10-25T00:30:00+00:00")
        await self.tick("2026-10-25T01:30:00+00:00")
        self.assertEqual(self.runtime.async_learn.await_count, 1)
        await self.tick("2027-03-28T00:59:00+00:00")
        self.assertEqual(self.runtime.async_learn.await_count, 1)
        await self.tick("2027-03-28T01:00:00+00:00")
        self.assertEqual(self.runtime.async_learn.await_count, 2)

    async def test_one_failure_does_not_stop_other_devices_and_removed_devices_are_skipped(self):
        self.configure()
        self.runtime.data["devices"]["b"] = {"entities": {"sensor.b": {}}}
        self.runtime.data["devices"]["removed"] = {"entities": {}}
        self.runtime.async_learn.side_effect = [
            ValueError("unavailable"),
            {"status": "insufficient", "id": "b"},
        ]
        await self.tick("2026-09-26T02:00:00+00:00")
        self.assertEqual(self.device["nightly_learning"]["status"], "error")
        self.assertEqual(
            self.runtime.data["devices"]["b"]["nightly_learning"]["status"], "insufficient"
        )
        self.assertEqual(self.runtime.async_learn.await_count, 2)
        self.hass.services.async_call.assert_not_awaited()

    async def test_overlapping_ticks_and_shutdown(self):
        self.configure()
        started = asyncio.Event()

        async def learning(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()

        self.runtime.async_learn.side_effect = learning
        await self.runtime.nightly_tick(datetime.fromisoformat("2026-09-26T02:00:00+00:00"))
        await started.wait()
        await self.runtime.nightly_tick(datetime.fromisoformat("2026-09-27T02:00:00+00:00"))
        self.assertEqual(self.runtime.async_learn.await_count, 1)
        self.runtime.unsub_nightly = Mock()
        await schedule.stop(self.runtime)
        self.runtime.unsub_nightly.assert_called_once()
        self.assertEqual(self.device["nightly_learning"]["status"], "interrupted")

    def test_config_validation_and_interrupted_restart(self):
        for enabled, at in ((True, "24:00"), (True, "3:00"), (True, "03:60"), ("yes", "03:00")):
            with self.assertRaises(ValueError):
                self.runtime.configure_learning_schedule(enabled, at)
        self.device["nightly_learning"] = {"status": "running"}
        schedule.restore(self.runtime)
        self.assertEqual(self.device["nightly_learning"]["status"], "interrupted")
