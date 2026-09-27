"""Learn recovery is bounded, optional and never applies thresholds."""

import asyncio
import types
import unittest
from unittest.mock import patch

import test_tuner as harness
from tuner_under_test.calibration import recovery


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = harness.RuntimeTests.asyncTearDown
    configuration = harness.RuntimeTests.configuration
    report_write = harness.RuntimeTests.report_write

    def setUp(self):
        harness.RuntimeTests.setUp(self)
        self.configuration()
        self.addCleanup(patch.stopall)
        patch.object(recovery, "RESTART_WAIT_SECONDS", 0).start()

    def entity(self, name, state="off", device="a", disabled=None):
        self.registry.entities[name] = types.SimpleNamespace(
            entity_id=name, domain=name.split(".")[0], device_id=device, disabled_by=disabled
        )
        self.states[name] = types.SimpleNamespace(state=state)
        return name

    def unavailable(self):
        self.states["number.radar_g0_still_threshold"].state = "unknown"

    def restore(self):
        self.states["number.radar_g0_still_threshold"].state = "10"

    def assert_no_threshold_writes(self):
        assert all(c.args[0] != "number" for c in self.hass.services.async_call.await_args_list)

    async def test_healthy_device_is_untouched(self):
        await recovery.prepare_learning(self.runtime, "a")
        self.hass.services.async_call.assert_not_awaited()

    async def test_query_recovers_before_any_restart(self):
        self.unavailable()
        self.entity("button.radar_query_params")
        self.entity("switch.radar_bluetooth")

        async def call(*args, **kwargs):
            self.restore()

        self.hass.services.async_call.side_effect = call
        await recovery.prepare_learning(self.runtime, "a")
        assert self.device["configuration_recovery"]["steps"] == ["query_parameters"]
        assert self.device["configuration_recovery"]["status"] == "recovered"
        self.assert_no_threshold_writes()

    async def test_radar_restart_restores_engineering_without_package(self):
        self.unavailable()
        self.entity("button.radar_restart")
        engineering = self.entity("switch.radar_engineering_mode", "on")

        async def call(domain, service, data, **kwargs):
            if domain == "button":
                self.restore()

        self.hass.services.async_call.side_effect = call
        await recovery.prepare_learning(self.runtime, "a")
        calls = self.hass.services.async_call.await_args_list
        assert calls[0].args[2]["entity_id"] == "button.radar_restart"
        assert calls[1].args == ("switch", "turn_on", {"entity_id": engineering})
        self.assert_no_threshold_writes()

    async def test_bluetooth_cycle_preserves_each_original_state(self):
        for original in ("on", "off"):
            self.unavailable()
            bluetooth = self.entity("switch.radar_bluetooth", original)
            self.device.pop("configuration_recovery", None)
            self.hass.services.async_call.reset_mock()

            async def call(domain, service, data, **kwargs):
                self.states[data["entity_id"]].state = service.removeprefix("turn_")
                self.restore()

            self.hass.services.async_call.side_effect = call
            await recovery.prepare_learning(self.runtime, "a")
            assert self.states[bluetooth].state == original
            assert self.hass.services.async_call.await_count == 2
            assert "restore_bluetooth" not in self.device["configuration_recovery"]
            self.assert_no_threshold_writes()

    async def test_failed_cycle_restores_bluetooth_and_reports_error(self):
        self.unavailable()
        bluetooth = self.entity("switch.radar_bluetooth")

        async def call(domain, service, data, **kwargs):
            self.states[bluetooth].state = service.removeprefix("turn_")

        self.hass.services.async_call.side_effect = call
        with self.assertRaisesRegex(ValueError, "still unavailable"):
            await recovery.prepare_learning(self.runtime, "a")
        assert self.states[bluetooth].state == "off"
        assert self.device["configuration_recovery"]["status"] == "failed"
        with self.assertRaisesRegex(ValueError, "cooling down"):
            await recovery.prepare_learning(self.runtime, "a")
        assert self.hass.services.async_call.await_count == 2
        self.device["configuration_recovery"]["retry_after"] = 0
        with self.assertRaises(ValueError):
            await recovery.prepare_learning(self.runtime, "a")
        assert self.hass.services.async_call.await_count == 4

    async def test_cancelled_cycle_restores_original_and_releases_lock(self):
        self.unavailable()
        bluetooth = self.entity("switch.radar_bluetooth")
        entered = asyncio.Event()

        async def call(domain, service, data, **kwargs):
            self.states[bluetooth].state = service.removeprefix("turn_")
            if service == "turn_on":
                entered.set()
                await asyncio.Future()

        self.hass.services.async_call.side_effect = call
        task = asyncio.create_task(recovery.prepare_learning(self.runtime, "a"))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        assert self.states[bluetooth].state == "off"
        assert self.device["configuration_recovery"]["status"] == "interrupted"
        assert not self.runtime._applying

    async def test_unknown_bluetooth_and_generic_restart_are_not_toggled(self):
        self.unavailable()
        self.entity("button.restart")
        self.entity("switch.radar_bluetooth", "unknown")
        with self.assertRaisesRegex(ValueError, "state is unknown"):
            await recovery.prepare_learning(self.runtime, "a")
        self.hass.services.async_call.assert_not_awaited()

    async def test_missing_capabilities_and_other_devices_fail_clearly(self):
        self.unavailable()
        self.entity("switch.other_bluetooth", device="b")
        self.entity("button.disabled_radar_restart", disabled="user")
        with self.assertRaisesRegex(ValueError, "still unavailable"):
            await recovery.prepare_learning(self.runtime, "a")
        self.hass.services.async_call.assert_not_awaited()
        assert self.device["configuration_recovery"]["error"]

    async def test_missing_entity_is_not_misdiagnosed_as_radar_bad_state(self):
        self.registry.entities.pop("number.radar_g0_still_threshold")
        self.entity("switch.radar_bluetooth")
        with self.assertRaises(ValueError):
            await recovery.prepare_learning(self.runtime, "a")
        self.hass.services.async_call.assert_not_awaited()

    async def test_failed_restoration_is_retained_for_next_attempt(self):
        self.unavailable()
        bluetooth = self.entity("switch.radar_bluetooth")

        async def call(domain, service, data, **kwargs):
            if service == "turn_on":
                self.states[bluetooth].state = "on"

        self.hass.services.async_call.side_effect = call
        with self.assertRaisesRegex(ValueError, "restoration was not confirmed"):
            await recovery.prepare_learning(self.runtime, "a")
        assert self.device["configuration_recovery"]["restore_bluetooth"]["state"] == "off"
        self.restore()

        async def restore(domain, service, data, **kwargs):
            self.states[bluetooth].state = "off"

        self.hass.services.async_call.side_effect = restore
        await recovery.prepare_learning(self.runtime, "a")
        assert "restore_bluetooth" not in self.device["configuration_recovery"]

    async def test_nightly_run_recovers_then_saves_without_applying(self):
        from tuner_under_test.runtime import schedule

        self.unavailable()
        self.entity("button.radar_query_params")

        async def call(*args, **kwargs):
            self.restore()

        self.hass.services.async_call.side_effect = call
        await schedule._learn_device(self.runtime, "a", self.device, "2026-09-27")
        assert self.device["configuration_recovery"]["status"] == "recovered"
        assert self.device["nightly_learning"]["status"] == "insufficient"
        assert "automatic" in self.device["learning_results"]
        self.assert_no_threshold_writes()

    def test_startup_unpauses_recording_but_retains_pending_restoration(self):
        from tuner_under_test.presence import autolabelling
        from tuner_under_test.runtime import schedule

        self.device["configuration_recovery"] = {
            "status": "running",
            "restore_bluetooth": {"entity_id": "switch.radar_bluetooth", "state": "off"},
        }
        with patch.object(autolabelling, "_read_energies") as read:
            autolabelling._sample_devices(self.runtime)
            read.assert_not_called()
        schedule.restore(self.runtime)
        assert self.device["configuration_recovery"]["status"] == "interrupted"
        assert self.device["configuration_recovery"]["restore_bluetooth"]["state"] == "off"

    async def test_missing_maximum_distance_is_recoverable(self):
        key = "number.radar_max_move_distance_gate"
        self.states[key].state = "unknown"
        self.entity("button.radar_query_params")

        async def call(*args, **kwargs):
            self.states[key].state = "2"

        self.hass.services.async_call.side_effect = call
        await recovery.prepare_learning(self.runtime, "a")
        assert self.device["configuration_recovery"]["status"] == "recovered"
        self.assert_no_threshold_writes()
