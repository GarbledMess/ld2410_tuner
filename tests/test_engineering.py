"""Automatic engineering mode uses standard entities and never changes gate settings."""

import asyncio
import types
import unittest
from unittest.mock import Mock, patch

import test_tuner as harness
from tuner_under_test.runtime import engineering


class EngineeringTests(unittest.IsolatedAsyncioTestCase):
    setUp = harness.RuntimeTests.setUp

    async def asyncTearDown(self):
        await engineering.stop(self.runtime)
        await harness.RuntimeTests.asyncTearDown(self)

    def entity(self, entity_id, state="off", device_id="a", **extra):
        self.registry.entities[entity_id] = types.SimpleNamespace(
            entity_id=entity_id, device_id=device_id, domain=entity_id.split(".")[0], **extra
        )
        self.states[entity_id] = types.SimpleNamespace(state=state)
        return entity_id

    async def report_switch(self, domain, service, data, **kwargs):
        assert (domain, service) == ("switch", "turn_on")
        self.states[data["entity_id"]].state = "on"

    async def check(self):
        engineering.tick(self.runtime)
        await self.runtime._engineering_task

    async def test_recording_radars_enable_independently_without_package(self):
        for index in range(12):
            device_id = f"radar_{index}"
            self.runtime.data["devices"][device_id] = {
                "entities": {"gate": {}},
                "recording_enabled": True,
            }
            self.entity(f"switch.node_{index}_engineering_mode", device_id=device_id)
        self.hass.services.async_call.side_effect = self.report_switch
        await self.check()
        assert self.hass.services.async_call.await_count == 12
        assert not self.runtime._engineering_status
        assert not self.runtime._applying
        await self.check()
        assert self.hass.services.async_call.await_count == 12

    async def test_renamed_switch_is_recognized_by_original_name(self):
        switch = self.entity("switch.custom", original_name="Engineering Mode")
        self.hass.services.async_call.side_effect = self.report_switch
        await self.check()
        assert self.states[switch].state == "on"

    async def test_on_unknown_unavailable_disabled_ambiguous_and_unrelated_are_untouched(self):
        for state in ("on", "unknown", "unavailable"):
            self.entity("switch.radar_engineering_mode", state)
            await self.check()
        self.entity("switch.radar_engineering_mode", disabled_by="user")
        await self.check()
        self.entity("switch.radar_engineering_mode")
        self.entity("switch.duplicate_engineering_mode")
        await self.check()
        self.registry.entities.clear()
        self.entity("switch.other_engineering_mode", device_id="unrelated")
        await self.check()
        self.hass.services.async_call.assert_not_awaited()

    async def test_busy_device_and_package_recovery_wait_without_spending_retries(self):
        self.entity("switch.radar_engineering_mode")
        self.runtime._applying.add("a")
        await self.check()
        self.runtime._applying.clear()
        sensor = self.entity("sensor.radar_ld2410_recovery_status", "Recovering missing settings")
        await self.check()
        self.hass.services.async_call.assert_not_awaited()
        assert not self.runtime._engineering_attempts
        assert self.runtime._engineering_status["a"]["status"] == "waiting"
        self.states[sensor].state = "Settings available"
        self.hass.services.async_call.side_effect = self.report_switch
        await self.check()
        self.hass.services.async_call.assert_awaited_once()

    async def test_failed_reports_back_off_and_reconnect_can_retry(self):
        switch = self.entity("switch.radar_engineering_mode")
        with patch.object(engineering.time, "monotonic", return_value=100) as clock:
            await self.check()
            assert self.runtime._engineering_attempts["a"]["retry_at"] == 700
            await self.check()
            self.hass.services.async_call.assert_awaited_once()
            clock.return_value = 700
            await self.check()
            assert self.runtime._engineering_attempts["a"]["retry_at"] == 1900
            clock.return_value = 1900
            await self.check()
            assert self.runtime._engineering_attempts["a"]["retry_at"] == 3700
            assert self.runtime._engineering_status["a"]["status"] == "retrying"
            self.states[switch].state = "unavailable"
            await self.check()
            assert not self.runtime._engineering_status
            self.states[switch].state = "off"
            self.hass.services.async_call.side_effect = self.report_switch
            clock.return_value = 3700
            await self.check()
            assert self.states[switch].state == "on"
            clock.return_value = 4301
            await self.check()
            assert not self.runtime._engineering_attempts

    async def test_service_failure_does_not_stop_other_radars(self):
        self.entity("switch.radar_engineering_mode")
        self.runtime.data["devices"]["b"] = {"entities": {"gate": {}}}
        self.entity("switch.second_engineering_mode", device_id="b")
        self.hass.services.async_call.side_effect = [RuntimeError("offline"), None]
        await self.check()
        assert self.hass.services.async_call.await_count == 2
        assert "offline" in self.runtime._engineering_status["a"]["error"]
        assert not self.runtime._applying

    async def test_no_overlapping_checks_and_unload_cancels_pending_enable(self):
        self.entity("switch.radar_engineering_mode")
        entered = asyncio.Event()

        async def blocked(*args, **kwargs):
            entered.set()
            await asyncio.Future()

        self.hass.services.async_call.side_effect = blocked
        self.runtime.unsub_engineering = Mock()
        unsubscribe = self.runtime.unsub_engineering
        engineering.tick(self.runtime)
        task = self.runtime._engineering_task
        await entered.wait()
        engineering.tick(self.runtime)
        assert self.runtime._engineering_task is task
        assert "a" in self.runtime._applying
        await engineering.stop(self.runtime)
        unsubscribe.assert_called_once()
        assert task.cancelled()
        assert not self.runtime._applying

    async def test_removed_device_is_not_written(self):
        self.entity("switch.radar_engineering_mode")
        self.device["entities"] = {}
        await self.check()
        self.hass.services.async_call.assert_not_awaited()

    async def test_optimistic_on_then_revert_does_not_immediately_resend(self):
        switch = self.entity("switch.radar_engineering_mode")
        self.hass.services.async_call.side_effect = self.report_switch
        with patch.object(engineering.time, "monotonic", return_value=100) as clock:
            await self.check()
            clock.return_value = 130
            await self.check()
            self.states[switch].state = "off"
            await self.check()
            self.hass.services.async_call.assert_awaited_once()
            assert self.runtime._engineering_status["a"]["status"] == "waiting"
            clock.return_value = 700
            await self.check()
            assert self.hass.services.async_call.await_count == 2

    async def test_subdevices_keep_gate_reads_timing_and_writes_separate(self):
        from tuner_under_test.calibration.timing_config import read_timing

        for index in range(3):
            device_id = f"subdevice_{index}"
            for gate in range(9):
                for kind in ("move", "still"):
                    self.entity(
                        f"sensor.hub_radar_{index}_g{gate}_{kind}_energy",
                        str(20 + index),
                        device_id,
                    )
                    self.entity(
                        f"number.hub_radar_{index}_g{gate}_{kind}_threshold",
                        str(30 + index),
                        device_id,
                    )
            self.entity(
                f"number.hub_radar_{index}_timeout",
                str(index + 1),
                device_id,
                original_name="Timeout",
            )
        self.runtime.refresh_devices(self.registry)
        for index in range(3):
            device_id = f"subdevice_{index}"
            device = self.runtime.data["devices"][device_id]
            assert len(device["entities"]) == 18
            assert all(f"radar_{index}_" in name for name in device["entities"])
            entities, values = self.runtime._threshold_configuration(device_id)
            assert len(entities) == 18
            assert set(values.values()) == {30 + index}
            assert read_timing(self.runtime, device_id)["timeout"] == index + 1
        await self.runtime.set_gate_threshold("subdevice_1", "g0_still", 55)
        self.hass.services.async_call.assert_awaited_once_with(
            "number",
            "set_value",
            {"entity_id": "number.hub_radar_1_g0_still_threshold", "value": 55},
            blocking=True,
        )
        assert self.states["number.hub_radar_0_g0_still_threshold"].state == "30"
        assert self.states["number.hub_radar_2_g0_still_threshold"].state == "32"

    async def test_only_recording_devices_are_enabled_including_legacy_default(self):
        active = self.entity("switch.radar_engineering_mode")
        self.runtime.data["devices"]["paused"] = {
            "entities": {"gate": {}},
            "recording_enabled": False,
        }
        paused = self.entity("switch.paused_engineering_mode", device_id="paused")
        self.hass.services.async_call.side_effect = self.report_switch
        # Existing devices without the newer setting still record by default.
        assert "recording_enabled" not in self.device
        await self.check()
        assert self.states[active].state == "on"
        assert self.states[paused].state == "off"
        self.hass.services.async_call.assert_awaited_once()
        self.runtime.data["devices"]["paused"]["recording_enabled"] = True
        await self.check()
        assert self.states[paused].state == "on"

    async def test_disabling_recording_stops_pending_retries(self):
        self.entity("switch.radar_engineering_mode")
        with patch.object(engineering.time, "monotonic", return_value=100) as clock:
            await self.check()
            assert self.runtime._engineering_status["a"]["status"] == "retrying"
            self.device["recording_enabled"] = False
            clock.return_value = 10000
            await self.check()
        self.hass.services.async_call.assert_awaited_once()
        assert not self.runtime._engineering_status

    async def test_rechecks_recording_after_waiting_for_another_device(self):
        self.entity("switch.radar_engineering_mode")
        self.runtime.data["devices"]["b"] = {"entities": {"gate": {}}, "recording_enabled": True}
        second = self.entity("switch.second_engineering_mode", device_id="b")

        async def call(*args, **kwargs):
            # Simulate a settings replacement while the first service call awaits.
            self.runtime.data["devices"]["b"] = {
                "entities": {"gate": {}},
                "recording_enabled": False,
            }
            await self.report_switch(*args, **kwargs)

        self.hass.services.async_call.side_effect = call
        await self.check()
        assert self.states[second].state == "off"
        self.hass.services.async_call.assert_awaited_once()

    async def test_startup_and_timer_use_ten_minute_checks(self):
        from datetime import timedelta

        with patch.object(engineering, "async_track_time_interval", return_value=Mock()) as track:
            engineering.start(self.runtime)
            await self.runtime._engineering_task
        assert track.call_args.args[2] == timedelta(minutes=10)
        track.call_args.args[1](None)
        await self.runtime._engineering_task
