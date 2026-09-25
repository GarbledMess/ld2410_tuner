"""Integration boundary checks with synthetic states and no live HA writes."""

import asyncio
import importlib
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

import test_tuner as harness

mod = harness.mod


class SurfaceTests(unittest.IsolatedAsyncioTestCase):
    setUp = harness.RuntimeTests.setUp
    asyncTearDown = harness.RuntimeTests.asyncTearDown
    sample = harness.RuntimeTests.sample
    configuration = harness.RuntimeTests.configuration

    def add_entity(self, entity_id, device_id="a"):
        entity = types.SimpleNamespace(
            entity_id=entity_id, device_id=device_id, domain=entity_id.split(".")[0]
        )
        self.registry.entities[entity_id] = entity
        return entity

    def test_discovery_refresh_and_state_updates_ignore_unrelated_entities(self):
        energy = "sensor.radar_g0_move_energy"
        self.add_entity(energy)
        self.add_entity("sensor.unrelated")
        self.add_entity("sensor.orphan_g1_move_energy", None)
        self.add_entity("number.radar_g0_move_threshold")
        self.registry.async_get = self.registry.entities.get
        self.runtime.refresh_devices(self.registry)
        self.assertEqual(list(self.device["entities"]), [energy])
        old = self.runtime.unsub = Mock()
        self.runtime.subscribe_state_changes()
        old.assert_called_once()
        for entity_id, state in [
            (energy, "30"),
            (energy, "unavailable"),
            (energy, "nan"),
            (energy, "-1"),
            ("sensor.unrelated", "9"),
            ("sensor.missing", "4"),
        ]:
            self.runtime.handle_state_change(
                types.SimpleNamespace(
                    data={"entity_id": entity_id, "new_state": types.SimpleNamespace(state=state)}
                )
            )
        self.runtime.handle_state_change(types.SimpleNamespace(data={"entity_id": energy}))
        self.assertEqual(self.runtime._live["a"], {"g0_move": 30})
        self.registry.entities.clear()
        self.runtime.handle_registry_update(None)
        self.assertEqual(self.device["entities"], {})

    def test_snapshot_and_export_preserve_training_and_hide_removed_devices(self):
        self.configuration()
        self.add_entity("sensor.radar_g0_move_energy")
        dr = sys.modules["homeassistant.helpers.device_registry"]
        dr.async_get = lambda _: types.SimpleNamespace(
            async_get=lambda _: types.SimpleNamespace(
                name_by_user="Synthetic room", name="Radar", area_id="test_area"
            )
        )
        self.device["samples"] = {"g0_move": {"present": [3, 3, 4], "not_present": [1]}}
        self.runtime.data["devices"]["removed"] = {"histograms": {}}
        self.states["number.radar_g1_move_threshold"].state = "unavailable"
        self.states["number.radar_g2_move_threshold"].state = "invalid"
        snapshot = self.runtime.snapshot()
        self.assertEqual(set(snapshot["devices"]), {"a"})
        view = snapshot["devices"]["a"]
        self.assertEqual(view["name"], "Synthetic room")
        self.assertEqual(view["sample_counts"]["g0_move"], {"present": 3, "not_present": 1})
        self.assertEqual(view["histogram_stats"]["g0_move"]["present"]["p50"], 3)
        self.assertEqual(view["current_thresholds"]["g0_move"], 10)
        self.assertNotIn("g1_move", view["current_thresholds"])
        exported = self.runtime.export_data("a")
        self.assertEqual(exported["devices"]["a"]["sample_counts"], view["sample_counts"])
        self.assertEqual(self.runtime.export_data("missing")["devices"], {})
        self.assertEqual(set(self.runtime.export_data()["devices"]), {"a", "removed"})

    def test_feedback_is_bounded_and_stale_guesses_are_rejected(self):
        for label, bias in [("present", "present_bias"), ("not_present", "absent_bias")]:
            self.device["auto"] = {
                "last_classification": {"state": label, "timestamp": self.now, "confidence": 0.8},
                "feedback": [{"label": label, "correct": True}] * 600,
            }
            result = self.runtime.record_auto_feedback("a", False)
            raised = result["calibration"][bias]
            result = self.runtime.record_auto_feedback("a", True)
            self.assertLess(result["calibration"][bias], raised)
            summary = self.runtime.auto_learning_summary(self.device)
            self.assertLessEqual(summary["feedback"][label]["total"], 500)
            self.assertEqual(summary["last"]["state"], label)
        self.device["auto"]["last_classification"]["timestamp"] -= 100
        self.assertIsNone(self.runtime.auto_learning_summary(self.device)["last"])
        for device_id in ("a", "missing"):
            with self.assertRaises(ValueError):
                self.runtime.record_auto_feedback(device_id, True)

    async def test_timeout_restoration_closes_label_at_original_expiry(self):
        self.device.update(
            training_state="present",
            training_label_start=self.now - 20,
            training_expires_at=self.now - 10,
        )
        self.runtime.restore_timeouts()
        await asyncio.gather(*self.runtime._timeout_tasks.values())
        self.assertEqual(self.device["training_state"], "unknown")
        self.assertEqual(self.device["history_labels"][0]["end"], self.now - 10)

    async def test_delayed_saves_are_shared(self):
        runtime_module = sys.modules["tuner_under_test.runtime.coordinator"]
        del self.runtime._schedule_save
        with patch.object(runtime_module, "STORE_DELAY", 0):
            self.runtime._schedule_save()
            task = self.runtime._save_task
            self.runtime._schedule_save()
            self.assertIs(self.runtime._save_task, task)
            await task
        self.runtime.store.async_save.assert_awaited_once_with(self.runtime.data)

    async def test_setup_reload_and_unload_register_resources_once(self):
        self.hass.data = {}
        self.hass.bus = types.SimpleNamespace(async_listen=Mock(return_value=Mock()))
        self.hass.http = types.SimpleNamespace(async_register_static_paths=AsyncMock())
        store = types.SimpleNamespace(
            async_load=AsyncMock(return_value={"devices": {}}), async_save=AsyncMock()
        )
        with (
            patch.object(mod, "TunerStore", return_value=store),
            patch.object(mod, "StaticPathConfig", Mock()),
            patch.object(mod, "_register_websocket_commands") as register,
            patch.object(mod.frontend, "async_panel_exists", return_value=False, create=True),
            patch.object(mod.frontend, "async_register_built_in_panel", create=True) as panel,
        ):
            self.assertTrue(await mod.async_setup_entry(self.hass, None))
            self.assertTrue(await mod.async_setup_entry(self.hass, None))
            register.assert_called_once()
            self.assertTrue(panel.call_args.kwargs["require_admin"])
            self.assertEqual(self.hass.http.async_register_static_paths.await_count, 1)
            runtime = self.hass.data[mod.DOMAIN]
            runtime._save_task = asyncio.create_task(asyncio.sleep(100))
            runtime._cleanup_task = asyncio.create_task(asyncio.sleep(100))
            runtime._timeout_tasks["test"] = asyncio.create_task(asyncio.sleep(100))
            await mod.async_unload_entry(self.hass, None)
            self.assertTrue(runtime._save_task.cancelled())
            self.assertTrue(runtime._cleanup_task.cancelled())
            self.assertEqual(runtime._timeout_tasks, {})
            self.assertNotIn(mod.DOMAIN, self.hass.data)
            await mod.async_setup_entry(self.hass, None)
            self.assertEqual(self.hass.http.async_register_static_paths.await_count, 1)
            await mod.async_unload_entry(self.hass, None)
        with (
            patch.object(mod.frontend, "async_panel_exists", return_value=True, create=True),
            patch.object(mod.frontend, "async_remove_panel", create=True) as remove,
        ):
            await mod.async_unload_entry(self.hass, None)
            remove.assert_called_once()
        original = {"devices": {}}
        self.assertIs(await mod.TunerStore()._async_migrate_func(1, 0, original), original)

    async def test_config_flow_single_instance_and_explicit_creation(self):
        class Flow:
            def __init_subclass__(cls, **kwargs):
                pass

        entries = sys.modules["homeassistant.config_entries"]
        with patch.object(entries, "ConfigFlow", Flow, create=True):
            module = importlib.import_module("tuner_under_test.config_flow")
        flow = module.LD2410TunerConfigFlow()
        flow._async_current_entries = Mock(return_value=[])
        flow.async_abort = Mock(return_value="abort")
        flow.async_show_form = Mock(return_value="form")
        flow.async_create_entry = Mock(return_value="entry")
        self.assertEqual(await flow.async_step_user(), "form")
        self.assertEqual(await flow.async_step_user({}), "entry")
        flow._async_current_entries.return_value = [object()]
        self.assertEqual(await flow.async_step_user({}), "abort")
        flow.async_abort.assert_called_once_with(reason="single_instance_allowed")

    async def test_cleanup_retains_device_inventory_snapshot_across_await(self):
        self.sample(self.now - 6)
        self.runtime._flush_history_block("a")
        added = {"entities": {}}

        async def execute(function, *args):
            self.runtime.data["devices"]["new"] = added
            return function(*args)

        self.hass.async_add_executor_job = execute
        await self.runtime.async_clean_history(persist=False)
        self.assertEqual(set(self.runtime.data["devices"]), {"a", "new"})
        self.assertNotIn("history_cleanup", added)
        self.assertIn("history_cleanup", self.device)
