"""Actual production learning route, with real Linux children in the HA lab."""

import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import test_automatic_weight as weight
import test_tuner as harness
from tuner_under_test.calibration import service
from tuner_under_test.worker import bridge, supervisor


class ProductionLearningTests(unittest.IsolatedAsyncioTestCase):
    setUp = harness.RuntimeTests.setUp
    asyncTearDown = harness.RuntimeTests.asyncTearDown
    configuration = harness.RuntimeTests.configuration
    labelled = weight.DeviceLearnTests.labelled

    async def test_real_learning_path_installs_controls_and_preserves_results(self):
        entities, current = self.labelled()
        expected = self.runtime._fit_history("a", entities, current)
        self.worker_patch.stop()
        original = asyncio.create_subprocess_exec
        controls = []
        beats = []
        finished = asyncio.Event()

        async def heartbeat():
            while not finished.is_set():
                beats.append(True)
                await asyncio.sleep(0.002)

        async def spawn(*args, **kwargs):
            process = await original(*args, **kwargs)
            if sys.platform == "linux":
                for _ in range(1000):
                    try:
                        limits = Path(f"/proc/{process.pid}/limits").read_text()
                        oom = int(Path(f"/proc/{process.pid}/oom_score_adj").read_text())
                        nice = os.getpriority(os.PRIO_PROCESS, process.pid)
                        if "536870912" in limits and oom >= 500 and nice == 19:
                            controls.append((limits, oom, nice))
                            break
                    except (FileNotFoundError, ProcessLookupError):
                        break
                    await asyncio.sleep(0.001)
            return process

        beat = asyncio.create_task(heartbeat())
        try:
            with (
                patch.object(
                    service.recovery,
                    "prepare_learning",
                    AsyncMock(return_value=(entities, current)),
                ),
                patch.object(service, "read_timing", return_value={}),
                patch.object(asyncio, "create_subprocess_exec", spawn),
            ):
                if sys.platform == "linux":
                    actual = await self.runtime._learn_once("a")
                    for key, value in expected.items():
                        if key != "created_at":
                            self.assertEqual(actual[key], value, key)
                    self.assertEqual(actual["label_revision"], 0)
                    self.assertTrue(
                        controls, "production child did not establish verified controls"
                    )
                    self.assertGreater(len(beats), 1)
                else:
                    with self.assertRaises(supervisor.WorkerFailed) as failure:
                        await self.runtime._learn_once("a")
                    self.assertIn(b"isolated Linux", failure.exception.stderr)
        finally:
            finished.set()
            await beat
        self.assertEqual(self.worker_transport.requests, [])
        await bridge.stop(self.runtime)
        self.assertFalse(self.runtime._worker_service._jobs)

    async def test_real_child_result_cannot_install_after_labels_are_cleared(self):
        entities, current = self.labelled()
        self.worker_patch.stop()
        original = asyncio.create_subprocess_exec

        async def spawn(*args, **kwargs):
            process = await original(*args, **kwargs)
            self.runtime.clear_samples("a")
            return process

        with (
            patch.object(
                service.recovery, "prepare_learning", AsyncMock(return_value=(entities, current))
            ),
            patch.object(service, "read_timing", return_value={}),
            patch.object(asyncio, "create_subprocess_exec", spawn),
        ):
            expected = (
                service.LearningEvidenceChanged
                if sys.platform == "linux"
                else supervisor.WorkerFailed
            )
            with self.assertRaises(expected):
                await self.runtime._learn_once("a")
        self.assertNotIn("last_learning", self.device)
        await bridge.stop(self.runtime)
        self.assertFalse(self.runtime._worker_service._jobs)

    async def test_initial_reference_import_also_uses_the_process_bridge(self):
        from tuner_under_test.presence import reference_training

        self.labelled()
        self.device.pop("reference_profile", None)
        await reference_training.initialize(self.runtime)
        self.assertEqual(self.worker_transport.requests[-1]["operation"], "references")
        self.assertTrue(self.device["reference_profile"]["periods"])
