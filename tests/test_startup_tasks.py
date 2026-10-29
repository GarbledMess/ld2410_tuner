"""Background work never holds up Home Assistant's start-up.

Home Assistant waits for every task made with ``hass.async_create_task`` before it
finishes starting; background workers, delayed saves and timers must not be among them.
"""

import ast
import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import test_tuner as harness

PACKAGE = Path(__file__).parents[1] / "custom_components" / "ld2410_tuner"


class StartupTaskTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        harness.RuntimeTests.setUp(self)
        self.background = []

        def tracked(target):
            target.close()
            raise AssertionError("Home Assistant start-up would wait for this task")

        def background(target, name):
            self.background.append(name)
            return asyncio.create_task(target)

        self.hass.async_create_task = tracked
        self.hass.async_create_background_task = background

    asyncTearDown = harness.RuntimeTests.asyncTearDown

    async def cancel(self, task):
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_delayed_save_is_background_work(self):
        coordinator = sys.modules["tuner_under_test.runtime.coordinator"]
        coordinator.TunerRuntime._schedule_save(self.runtime)
        self.assertEqual(len(self.background), 1)
        await self.cancel(self.runtime._save_task)

    async def test_seen_by_refresh_is_background_work(self):
        seen_by = sys.modules["tuner_under_test.rooms.seen_by"]

        async def run(*args):
            await asyncio.sleep(3600)

        with (
            patch.object(seen_by, "_has_recording", return_value=True),
            patch.object(seen_by, "_run", run),
        ):
            seen_by._refresh(self.runtime, {}, "area:office", {}, 0)
        self.assertEqual(len(self.background), 1)
        await self.cancel(self.runtime._room_attribution_jobs["area:office"])


def test_no_integration_task_is_tracked_by_start_up():
    tracked = [
        f"{path.relative_to(PACKAGE)}:{node.lineno}"
        for path in PACKAGE.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Attribute) and node.attr == "async_create_task"
    ]
    assert tracked == []
