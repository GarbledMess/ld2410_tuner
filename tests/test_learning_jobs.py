"""Learning belongs to the server, even when every browser waiter disappears."""

import asyncio
import json
import sys
import unittest
from copy import deepcopy
from unittest.mock import Mock, patch

import test_tuner as harness

jobs = sys.modules["tuner_under_test.calibration.jobs"]
schedule = sys.modules["tuner_under_test.runtime.schedule"]
presentation = sys.modules["tuner_under_test.presentation.snapshots"]


class LearningJobTests(unittest.IsolatedAsyncioTestCase):
    setUp = harness.RuntimeTests.setUp
    configuration = harness.RuntimeTests.configuration

    async def asyncTearDown(self):
        await schedule.stop(self.runtime)
        await harness.RuntimeTests.asyncTearDown(self)

    def pending_fit(self, error=None):
        self.configuration()
        learned = deepcopy(self.device.pop("last_learning"))
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.calls = 0
        self.runtime._schedule_save = Mock()

        async def execute(*args):
            self.calls += 1
            self.entered.set()
            await self.release.wait()
            if error:
                raise error
            return learned

        self.hass.async_add_executor_job = execute

    async def test_cancelled_request_still_saves_and_exposes_result(self):
        self.pending_fit()
        caller = asyncio.create_task(self.runtime.async_learn("a"))
        await self.entered.wait()
        task = self.runtime._learning_jobs["a"]
        caller.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await caller
        self.assertFalse(task.cancelled())
        with patch.object(presentation, "_snapshot_identity", return_value=("Radar", None)):
            report = presentation._snapshot_device(self.runtime, "a", self.device, self.registry)[
                "learning_job"
            ]
        self.assertEqual(report["status"], "running")
        self.assertEqual(report["stage"], "fitting")
        self.release.set()
        saved = await task
        self.assertEqual(report["status"], "completed")
        self.assertEqual(report["results"]["user"], saved["user"]["id"])
        self.assertEqual(self.device["last_learning"], saved["user"])
        self.assertEqual(self.runtime._learning_jobs, {})
        self.assertGreaterEqual(report["finished_at"], report["started_at"])
        self.runtime._schedule_save.assert_called()
        persisted = json.loads(json.dumps(self.runtime.data))
        restored = harness.mod.TunerRuntime(self.hass, self.runtime.store, persisted)
        schedule.restore(restored)
        self.assertEqual(persisted["devices"]["a"]["learning_job"], report)
        self.hass.services.async_call.assert_not_awaited()

    async def test_start_and_duplicate_requests_join_one_fit_and_save_both_sources(self):
        self.pending_fit()
        first = self.runtime.start_learning("a")
        second = self.runtime.start_learning("a")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["status"], "running")
        await self.entered.wait()
        task = jobs.start(self.runtime, "a", "automatic")
        first["sources"].append("tampered")
        self.release.set()
        saved = await task
        self.assertEqual(self.calls, 1)
        self.assertEqual(set(saved), {"user", "automatic"})
        self.assertNotEqual(saved["user"]["id"], saved["automatic"]["id"])
        self.assertEqual(set(self.device["learning_results"]), {"user", "automatic"})
        self.assertEqual(self.device["learning_job"]["sources"], ["user", "automatic"])
        self.hass.services.async_call.assert_not_awaited()

    async def test_error_is_recorded_without_a_waiting_browser_and_retry_replaces_it(self):
        self.pending_fit(ValueError("Radar configuration unavailable"))
        first = self.runtime.start_learning("a")
        await self.entered.wait()
        task = self.runtime._learning_jobs["a"]
        self.release.set()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.device["learning_job"]["status"], "error")
        self.assertIn("configuration unavailable", self.device["learning_job"]["error"])
        self.assertNotIn("last_learning", self.device)
        self.assertEqual(self.runtime._learning_jobs, {})
        self.pending_fit()
        second = self.runtime.start_learning("a")
        self.assertNotEqual(first["id"], second["id"])
        task = self.runtime._learning_jobs["a"]
        self.release.set()
        await task
        self.assertEqual(self.device["learning_job"]["status"], "completed")

    async def test_shutdown_marks_started_and_not_yet_started_jobs_interrupted(self):
        self.pending_fit()
        self.runtime.start_learning("a")
        await self.entered.wait()
        await schedule.stop(self.runtime)
        self.assertEqual(self.device["learning_job"]["status"], "interrupted")
        self.assertNotIn("last_learning", self.device)
        self.runtime.start_learning("a")
        await schedule.stop(self.runtime)
        self.assertEqual(self.device["learning_job"]["status"], "interrupted")
        self.assertEqual(self.runtime._learning_jobs, {})

    async def test_clear_during_fit_cannot_resurrect_results(self):
        self.pending_fit()
        self.runtime.start_learning("a")
        await self.entered.wait()
        task = self.runtime._learning_jobs["a"]
        self.runtime.clear_samples("a")
        self.release.set()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.device["learning_job"]["status"], "error")
        self.assertIn("labels changed", self.device["learning_job"]["error"])
        self.assertNotIn("learning_results", self.device)
        self.runtime.clear_samples("a")
        self.assertNotIn("learning_job", self.device)

    def test_restart_marks_stale_job_interrupted_and_validation_creates_no_job(self):
        for device, source in (("missing", "user"), ("a", "invalid")):
            with self.assertRaises(ValueError):
                jobs.start(self.runtime, device, source)
        self.assertEqual(self.runtime._learning_jobs, {})
        self.device["learning_job"] = {"status": "running", "started_at": 1}
        schedule.restore(self.runtime)
        self.assertEqual(self.device["learning_job"]["status"], "interrupted")
        self.assertIn("restarted", self.device["learning_job"]["error"])
        self.assertIn("finished_at", self.device["learning_job"])
