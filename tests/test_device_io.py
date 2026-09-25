"""Radar communication regressions, including optimistic ESPHome state echoes."""

import asyncio
import sys
import types
import unittest
from unittest.mock import patch

import test_tuner as harness

io = sys.modules["tuner_under_test.calibration.device_io"]


class DeviceIOTests(unittest.IsolatedAsyncioTestCase):
    setUp = harness.RuntimeTests.setUp
    asyncTearDown = harness.RuntimeTests.asyncTearDown
    configuration = harness.RuntimeTests.configuration
    report_write = harness.RuntimeTests.report_write

    def add_query(self, device_id="a", name="button.radar_query_params", disabled=None):
        self.registry.entities[name] = types.SimpleNamespace(
            entity_id=name, domain="button", device_id=device_id, disabled_by=disabled
        )
        return name

    def writes(self):
        return [
            call
            for call in self.hass.services.async_call.await_args_list
            if call.args[0] == "number"
        ]

    async def test_missing_states_without_query_send_nothing(self):
        self.configuration()
        self.states["number.radar_g0_still_threshold"].state = "unknown"
        with self.assertRaisesRegex(ValueError, "no thresholds were written"):
            await self.runtime.apply("a")
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.runtime._applying, set())

    async def test_manual_write_requires_the_other_half_of_gate(self):
        self.configuration()
        self.states["number.radar_g0_still_threshold"].state = "unavailable"
        with self.assertRaisesRegex(ValueError, "no thresholds were written"):
            await self.runtime.set_gate_threshold("a", "g0_move", 30)
        self.assertEqual(self.writes(), [])

    async def test_query_recovers_missing_settings_before_writing(self):
        self.configuration()
        self.add_query()
        self.states["number.radar_g0_still_threshold"].state = "unknown"

        async def recover(domain, service, data, **kwargs):
            if domain == "button" and not self.writes():
                self.states["number.radar_g0_still_threshold"].state = "10"
            await self.report_write(domain, service, data, **kwargs)

        self.hass.services.async_call.side_effect = recover
        result = await self.runtime.apply("a")
        self.assertEqual(len(result["applied"]), 6)
        self.assertEqual(result["skipped"], {})
        self.assertEqual(result["verification"], "reported_state")
        self.assertEqual(self.hass.services.async_call.await_args_list[0].args[0], "button")
        self.assertEqual(len(self.writes()), 6)

    async def test_failed_recovery_is_bounded_and_never_toggles_bluetooth(self):
        self.configuration()
        self.add_query()
        self.states["number.radar_g0_still_threshold"].state = "unknown"
        with self.assertRaisesRegex(ValueError, "no thresholds were written"):
            await self.runtime.apply("a")
        calls = self.hass.services.async_call.await_args_list
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(call.args[:2] == ("button", "press") for call in calls))

    async def test_refresh_changed_configuration_requires_new_preview(self):
        self.configuration()
        self.add_query()
        self.states["number.radar_g0_still_threshold"].state = "unknown"

        async def refresh(*args, **kwargs):
            self.states["number.radar_g0_move_threshold"].state = "11"
            self.states["number.radar_g0_still_threshold"].state = "10"

        self.hass.services.async_call.side_effect = refresh
        with self.assertRaisesRegex(ValueError, "configuration changed"):
            await self.runtime.apply("a")
        self.assertEqual(self.writes(), [])

    async def test_silent_drop_in_healthy_state_is_not_success(self):
        self.configuration()
        self.hass.services.async_call.side_effect = None
        result = await self.runtime.apply("a")
        self.assertEqual(result["applied"], {})
        self.assertEqual(len(result["skipped"]), 6)
        self.assertEqual(len(self.writes()), 1)
        self.assertIn("requested 20, device reports 10", result["skipped"]["g0_move"])

    async def test_optimistic_echo_then_query_reversion_is_not_success(self):
        self.configuration()
        self.add_query()

        async def revert(domain, service, data, **kwargs):
            await self.report_write(domain, service, data, **kwargs)
            if domain == "button":
                self.states["number.radar_g0_move_threshold"].state = "10"

        self.hass.services.async_call.side_effect = revert
        result = await self.runtime.apply("a")
        self.assertEqual(result["applied"], {})
        self.assertEqual(len(self.writes()), 1)

    async def test_later_pair_write_reverting_earlier_gate_removes_success(self):
        self.configuration()

        async def revert(domain, service, data, **kwargs):
            await self.report_write(domain, service, data, **kwargs)
            if len(self.writes()) == 2:
                self.states["number.radar_g0_move_threshold"].state = "10"

        self.hass.services.async_call.side_effect = revert
        result = await self.runtime.apply("a")
        self.assertNotIn("g0_move", result["applied"])
        self.assertIn("g0_move", result["skipped"])
        self.assertEqual(len(self.writes()), 2)

    async def test_unchanged_values_are_not_rewritten(self):
        self.configuration()
        for proposal in self.device["last_learning"]["proposals"].values():
            proposal["threshold"] = 10
        result = await self.runtime.apply("a")
        self.assertEqual(len(result["applied"]), 6)
        self.assertEqual(self.writes(), [])

    async def test_manual_writes_share_apply_guard_and_release_on_cancel(self):
        self.configuration()
        entered = asyncio.Event()

        async def blocked(*args, **kwargs):
            entered.set()
            await asyncio.Future()

        self.hass.services.async_call.side_effect = blocked
        task = asyncio.create_task(self.runtime.set_gate_threshold("a", "g0_move", 30))
        await entered.wait()
        with self.assertRaisesRegex(ValueError, "already in progress"):
            await self.runtime.apply("a")
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.runtime._applying, set())

    async def test_unresponsive_service_times_out_and_stops(self):
        self.configuration()

        async def stalled(*args, **kwargs):
            await asyncio.Future()

        self.hass.services.async_call.side_effect = stalled
        with patch.object(io, "SERVICE_TIMEOUT", 0.001):
            result = await self.runtime.apply("a")
        self.assertEqual(result["applied"], {})
        self.assertEqual(len(self.writes()), 1)

    def test_query_discovery_ignores_other_devices_and_disabled_buttons(self):
        self.add_query("b")
        self.add_query(name="button.disabled_query_params", disabled="user")
        self.assertIsNone(io.query_button(self.runtime, "a"))
        self.add_query(name="button.valid_query_params")
        self.assertEqual(io.query_button(self.runtime, "a"), "button.valid_query_params")
        self.add_query(name="button.other_query_parameters")
        with self.assertRaisesRegex(ValueError, "Multiple Query"):
            io.query_button(self.runtime, "a")

    def test_distance_limits_support_names_in_user_configuration(self):
        self.configuration()
        for kind in ("move", "still"):
            old = f"number.radar_max_{kind}_distance_gate"
            new = old.removesuffix("_gate")
            entry = self.registry.entities.pop(old)
            entry.entity_id = new
            self.registry.entities[new] = entry
            self.states[new] = self.states.pop(old)
        entities, _ = self.runtime._threshold_configuration("a")
        self.assertEqual(len(entities), 6)
        self.states["number.radar_max_move_distance"].state = "unknown"
        with self.assertRaisesRegex(ValueError, "Maximum distance"):
            self.runtime._threshold_configuration("a")

    async def test_invalid_manual_threshold_never_calls_device(self):
        self.configuration()
        for key, value in [("g9_move", 10), ("g0_move", float("nan")), ("g0_move", 101)]:
            with self.assertRaises(ValueError):
                await self.runtime.set_gate_threshold("a", key, value)
        self.assertEqual(self.writes(), [])

    async def test_label_change_during_recovery_prevents_all_writes(self):
        self.configuration()
        self.device["last_learning"]["label_revision"] = 0
        self.add_query()
        self.states["number.radar_g0_still_threshold"].state = "unknown"

        async def recover(*args, **kwargs):
            self.states["number.radar_g0_still_threshold"].state = "10"
            self.device["label_revision"] = 1

        self.hass.services.async_call.side_effect = recover
        with self.assertRaisesRegex(ValueError, "labels changed"):
            await self.runtime.apply("a")
        self.assertEqual(self.writes(), [])

    async def test_configuration_disappearing_mid_apply_stops_next_write(self):
        self.configuration()

        async def disappear(domain, service, data, **kwargs):
            await self.report_write(domain, service, data, **kwargs)
            self.states["number.radar_g1_still_threshold"].state = "unavailable"

        self.hass.services.async_call.side_effect = disappear
        result = await self.runtime.apply("a")
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(len(result["applied"]), 1)
        self.assertIn("became unavailable", result["skipped"]["g1_move"])
