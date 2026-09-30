"""Device overrides inherit changing defaults and gate every automatic write path."""

import sys
import types
import unittest
from copy import deepcopy
from unittest.mock import AsyncMock, patch

import pytest
import test_automatic_apply as harness

automatic = harness.automatic
schedule = harness.schedule
presentation = sys.modules["tuner_under_test.presentation.snapshots"]
websocket = sys.modules["tuner_under_test.runtime.websocket"]


@pytest.mark.parametrize("default", ["off", "overnight", "all"])
@pytest.mark.parametrize("override", ["inherit", "off", "overnight", "all"])
def test_effective_policy_and_device_isolation(default, override):
    runtime = types.SimpleNamespace(
        data={
            "learning_schedule": {
                "enabled": True,
                "auto_apply": default != "off",
                "auto_apply_scope": default,
            },
            "devices": {"a": {"auto_apply_override": override}, "b": {}},
        }
    )
    mode = default if override == "inherit" else override
    assert automatic.policy(runtime, "a") == {
        "override": override,
        "global": default,
        "effective": mode,
    }
    assert automatic.enabled(runtime, "user", "a") == (mode == "all")
    assert automatic.enabled(runtime, "automatic", "a") == (mode != "off")
    assert automatic.policy(runtime, "b")["effective"] == default
    runtime.data["learning_schedule"]["enabled"] = False
    assert not automatic.enabled(runtime, "automatic", "a")
    assert automatic.enabled(runtime, "user", "a") == (mode == "all")


class DeviceApplyPolicyTests(unittest.IsolatedAsyncioTestCase):
    setUp = harness.AutomaticApplyTests.setUp
    asyncTearDown = harness.AutomaticApplyTests.asyncTearDown
    configuration = harness.AutomaticApplyTests.configuration
    run_apply = harness.AutomaticApplyTests.run_apply

    async def test_manual_override_can_enable_apply_when_global_default_and_schedule_are_off(self):
        self.runtime.configure_learning_schedule(False, "03:00", False, "overnight")
        self.runtime.configure_device_auto_apply("a", "all")
        self.runtime._learn_once = AsyncMock(return_value=deepcopy(self.learned))
        await self.runtime.async_learn("a")
        assert self.device["learning_job"]["automatic_apply"]["status"] == "applied"
        assert self.hass.services.async_call.await_count == 6

    async def test_off_override_blocks_manual_and_overnight_without_blocking_manual_apply(self):
        self.runtime.configure_learning_schedule(True, "03:00", True, "all")
        self.runtime.configure_device_auto_apply("a", "off")
        self.runtime._learn_once = AsyncMock(return_value=deepcopy(self.learned))
        await self.runtime.async_learn("a")
        await schedule._learn_device(self.runtime, "a", self.device, "2026-09-30")
        assert self.device["nightly_learning"]["automatic_apply"]["status"] == "skipped"
        self.hass.services.async_call.assert_not_awaited()
        result = await self.runtime.apply(
            "a", "automatic", self.learned["id"], self.learned["configuration"]
        )
        assert not result["skipped"]
        assert self.hass.services.async_call.await_count == 6

    async def test_overnight_override_keeps_manual_preview_and_allows_scheduled_apply(self):
        self.runtime.configure_learning_schedule(True, "03:00", False, "all")
        self.runtime.configure_device_auto_apply("a", "overnight")
        self.runtime._learn_once = AsyncMock(return_value=deepcopy(self.learned))
        await self.runtime.async_learn("a")
        self.hass.services.async_call.assert_not_awaited()
        await schedule._learn_device(self.runtime, "a", self.device, "2026-09-30")
        assert self.device["nightly_learning"]["automatic_apply"]["status"] == "applied"

    async def test_override_change_during_comparison_stops_application(self):
        self.runtime.configure_device_auto_apply("a", "all")
        self.runtime._learn_once = AsyncMock(return_value=deepcopy(self.learned))

        async def execute(fn, *args):
            measured = fn(*args)
            self.runtime.configure_device_auto_apply("a", "off")
            return measured

        self.hass.async_add_executor_job = execute
        await self.runtime.async_learn("a")
        assert self.device["learning_job"]["automatic_apply"]["status"] == "skipped"
        self.hass.services.async_call.assert_not_awaited()

    async def test_override_change_mid_write_stops_remaining_gates(self):
        self.runtime.configure_device_auto_apply("a", "all")
        self.runtime._learn_once = AsyncMock(return_value=deepcopy(self.learned))

        async def write(*args, **kwargs):
            await self.report_write(*args, **kwargs)
            self.runtime.configure_device_auto_apply("a", "off")

        self.hass.services.async_call.side_effect = write
        await self.runtime.async_learn("a")
        assert self.device["learning_job"]["automatic_apply"]["status"] == "partial"
        assert self.hass.services.async_call.await_count == 1
        assert "current" not in self.device["learning_results"]

    async def test_override_persists_and_inherit_removes_it_without_copying_defaults(self):
        self.runtime.configure_device_auto_apply("a", "off")
        await self.runtime.async_save()
        saved = self.runtime.store.async_save.call_args.args[0]
        restored = types.SimpleNamespace(data=saved)
        assert automatic.policy(restored, "a")["effective"] == "off"
        self.runtime.configure_learning_schedule(True, "03:00", True, "all")
        assert automatic.policy(self.runtime, "a")["effective"] == "off"
        self.runtime.clear_samples("a")
        assert automatic.policy(self.runtime, "a")["effective"] == "off"
        self.runtime.configure_device_auto_apply("a", "inherit")
        assert "auto_apply_override" not in self.device
        assert automatic.policy(self.runtime, "a")["effective"] == "all"
        self.runtime.configure_learning_schedule(True, "03:00", False)
        assert automatic.policy(self.runtime, "a")["effective"] == "off"

    async def test_websocket_snapshot_and_export_expose_override_and_effective_default(self):
        result = await websocket._websocket_result(
            self.runtime,
            "configure_device_auto_apply",
            ("device_id", "mode"),
            {"device_id": "a", "mode": "off"},
        )
        assert result == {"override": "off", "global": "overnight", "effective": "off"}
        with patch.object(presentation, "_snapshot_identity", return_value=("Radar", None)):
            snapshot = presentation._snapshot_device(self.runtime, "a", self.device, self.registry)
        assert snapshot["automatic_apply_policy"] == result
        exported = presentation._export_device(
            self.runtime, "a", types.SimpleNamespace(async_get=lambda _: None), self.registry
        )
        assert exported["automatic_apply_policy"] == result
        for mode in (None, True, "invalid", {}, []):
            before = deepcopy(self.runtime.data)
            with pytest.raises(ValueError):
                self.runtime.configure_device_auto_apply("a", mode)
            assert self.runtime.data == before
        with pytest.raises(ValueError, match="Unknown device"):
            self.runtime.configure_device_auto_apply("missing", "all")
