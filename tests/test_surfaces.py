"""Integration boundary checks with synthetic states and no live HA writes."""

import asyncio
import importlib
import sys
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest
import test_tuner as harness

mod = harness.mod


class SurfaceTests(unittest.IsolatedAsyncioTestCase):
    setUp = harness.RuntimeTests.setUp
    asyncTearDown = harness.RuntimeTests.asyncTearDown
    sample = harness.RuntimeTests.sample
    configuration = harness.RuntimeTests.configuration

    async def initialization(self):
        task = self.hass.data.get(f"{mod.DOMAIN}_startup")
        if task is not None:
            await task

    async def test_setup_returns_while_history_initializes_and_unload_cancels_it(self):
        self.hass.data = {}
        started, release = asyncio.Event(), asyncio.Event()

        async def initialize(hass):
            started.set()
            await release.wait()

        with (
            patch.object(mod, "_start_runtime", side_effect=initialize),
            patch.object(mod.frontend, "async_panel_exists", return_value=False, create=True),
        ):
            try:
                self.assertTrue(await asyncio.wait_for(mod.async_setup_entry(self.hass, None), 1))
                await started.wait()
                task = self.hass.data[f"{mod.DOMAIN}_startup"]
                await mod.async_setup_entry(self.hass, None)
                self.assertIs(self.hass.data[f"{mod.DOMAIN}_startup"], task)
                await mod.async_unload_entry(self.hass, None)
                self.assertTrue(task.cancelled())
                self.assertNotIn(mod.DOMAIN, self.hass.data)
            finally:
                release.set()

    async def test_failed_initialization_is_reported_and_can_be_retried(self):
        self.hass.data = {}
        with (
            patch.object(mod, "_start_runtime", side_effect=RuntimeError("synthetic failure")),
            self.assertLogs(mod.__name__, level="ERROR") as logs,
        ):
            await mod.async_setup_entry(self.hass, None)
            task = self.hass.data[f"{mod.DOMAIN}_startup"]
            with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
                await task
            await asyncio.sleep(0)
        self.assertIn("reload the integration", logs.output[0])
        self.assertNotIn(f"{mod.DOMAIN}_startup", self.hass.data)
        with patch.object(mod, "_start_runtime", new=AsyncMock()) as initialize:
            await mod.async_setup_entry(self.hass, None)
            await self.initialization()
            initialize.assert_awaited_once()

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

    def registry_device(self):
        self.configuration()
        self.add_entity("sensor.radar_g0_move_energy")
        dr = sys.modules["homeassistant.helpers.device_registry"]
        dr.async_get = lambda _: types.SimpleNamespace(
            async_get=lambda _: types.SimpleNamespace(
                name_by_user="Synthetic room", name="Radar", area_id="test_area"
            )
        )

    async def test_export_builds_recorded_days_off_the_event_loop(self):
        import base64

        from tuner_under_test.history import dayfile
        from tuner_under_test.worker import display

        self.registry_device()
        self.runtime._record_history_sample("a", {"g0_move": 37}, self.now - 6)
        loop_thread = threading.get_ident()

        def encoded_days(view, device_id, real=display.recorded_days):
            self.assertNotEqual(threading.get_ident(), loop_thread)
            return real(view, device_id)

        with patch.object(display, "recorded_days", side_effect=encoded_days) as encoded:
            exported = await self.runtime.async_export_data("a")
        self.assertEqual(encoded.call_count, 1)
        files = exported["devices"]["a"]["history"]["day_files"]
        self.assertEqual(len(files), 1)
        recorded = dayfile.decode_day(base64.b64decode(next(iter(files.values()))))
        self.assertEqual(recorded.columns[0][0], 37)
        self.assertEqual(self.worker_transport.requests[-1]["operation"], "export_days")
        self.assertEqual((await self.runtime.async_export_data("missing"))["devices"], {})
        self.assertNotIn(
            "day_files", (await self.runtime.async_export_data())["devices"]["a"]["history"]
        )

    async def test_periodic_sampling_yields_to_home_assistant_between_radars(self):
        from copy import deepcopy

        self.runtime.data["devices"].update({key: deepcopy(self.device) for key in ("b", "c")})
        self.states["sensor.radar_g0_move_energy"] = types.SimpleNamespace(state="20")
        trace = []

        def recorded(device_id, *args):
            trace.append(device_id)
            asyncio.get_running_loop().call_soon(trace.append, "yield")

        self.runtime._record_history_sample = recorded
        await self.runtime.async_sample_devices()
        self.assertEqual(trace, ["a", "yield", "b", "yield", "c", "yield"])

    async def test_periodic_sampling_does_not_overlap_and_stops_between_radars(self):
        from copy import deepcopy

        self.runtime.data["devices"]["b"] = deepcopy(self.device)
        self.states["sensor.radar_g0_move_energy"] = types.SimpleNamespace(state="20")
        seen, duplicates = [], []

        def recorded(device_id, *args):
            seen.append(device_id)
            duplicates.append(asyncio.create_task(self.runtime.async_sample_devices()))
            asyncio.get_running_loop().call_soon(setattr, self.runtime, "_sampling_stopped", True)

        self.runtime._record_history_sample = recorded
        await self.runtime.async_sample_devices()
        await asyncio.gather(*duplicates)
        self.assertEqual(seen, ["a"])
        self.assertFalse(self.runtime._sampling_active)
        await self.runtime.async_sample_devices()
        self.assertEqual(seen, ["a"])

    def test_export_contains_day_history_for_selected_device(self):
        import base64

        from tuner_under_test.history import dayfile, spans

        self.registry_device()
        rows = [(self.now - 600 + i * 6, bytes([20 + i % 3] + [255] * 17)) for i in range(50)]
        harness.record_rows(self.runtime, "a", rows)
        exported = self.runtime.export_data("a")["devices"]["a"]["history"]
        days = exported["day_files"]
        assert days, "the selected radar's recorded days are exported"
        decoded = [dayfile.decode_day(base64.b64decode(data)) for data in days.values()]
        found = [(c.start, c.end, c.values) for d in decoded for c in spans.day_spans(d)]
        expected = [(c.start, c.end, c.values) for c in spans.events(self.runtime, "a")]
        assert found == expected
        # Exporting every radar keeps only the summaries: day files can be large.
        assert "day_files" not in self.runtime.export_data()["devices"]["a"]["history"]

    def test_snapshot_history_summary_from_day_store(self):
        self.registry_device()
        harness.record_rows(self.runtime, "a", [(self.now - 60, bytes([20] + [255] * 17))])
        history = self.runtime.snapshot()["devices"]["a"]["history"]
        assert history["days"] == 1
        assert history["oldest_day"] is not None
        # The last reading holds half a sample; day files keep milliseconds.
        assert history["newest"] == pytest.approx(self.now - 57, abs=1e-3)

    def test_snapshot_and_export_preserve_training_and_hide_removed_devices(self):
        self.configuration()
        self.add_entity("sensor.radar_g0_move_energy")
        dr = sys.modules["homeassistant.helpers.device_registry"]
        dr.async_get = lambda _: types.SimpleNamespace(
            async_get=lambda _: types.SimpleNamespace(
                name_by_user="Synthetic room", name="Radar", area_id="test_area"
            )
        )
        # Readings 3, 3 and 4 labelled present and 1 labelled empty, as histogram bins.
        present, empty = [0] * 101, [0] * 101
        present[3], present[4], empty[1] = 2, 1, 1
        self.device["histograms"] = {"g0_move": {"present": present, "not_present": empty}}
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
            patch.object(mod, "TunerStore", return_value=store) as made,
            patch.object(mod, "StaticPathConfig", Mock()),
            patch.object(mod, "_register_websocket_commands") as register,
            patch.object(mod.frontend, "async_panel_exists", return_value=False, create=True),
            patch.object(mod.frontend, "async_register_built_in_panel", create=True) as panel,
        ):
            self.assertTrue(await mod.async_setup_entry(self.hass, None))
            await self.initialization()
            # The store's version is what makes Home Assistant run the guess-run migration.
            self.assertEqual(made.call_args.kwargs["minor_version"], mod.STORAGE_MINOR_VERSION)
            self.assertTrue(await mod.async_setup_entry(self.hass, None))
            register.assert_called_once()
            self.assertTrue(panel.call_args.kwargs["require_admin"])
            custom = panel.call_args.kwargs["config"]["_panel_custom"]
            self.assertEqual(
                custom["module_url"],
                f"/api/ld2410_tuner/static/ld2410-tuner-panel.js?v={mod.INTEGRATION_VERSION}",
            )
            self.assertNotIn("js_url", custom)
            self.assertEqual(self.hass.http.async_register_static_paths.await_count, 1)
            runtime = self.hass.data[mod.DOMAIN]
            self.assertEqual(
                runtime._days.root,
                Path(self.hass.config.path(".storage", "ld2410_tuner", "history")),
            )
            runtime._save_task = asyncio.create_task(asyncio.sleep(100))
            runtime._cleanup_task = asyncio.create_task(asyncio.sleep(100))
            runtime._timeout_tasks["test"] = asyncio.create_task(asyncio.sleep(100))
            await mod.async_unload_entry(self.hass, None)
            self.assertTrue(runtime._save_task.cancelled())
            self.assertTrue(runtime._cleanup_task.cancelled())
            self.assertEqual(runtime._timeout_tasks, {})
            self.assertNotIn(mod.DOMAIN, self.hass.data)
            await mod.async_setup_entry(self.hass, None)
            await self.initialization()
            self.assertEqual(self.hass.http.async_register_static_paths.await_count, 1)
            await mod.async_unload_entry(self.hass, None)
        with (
            patch.object(mod.frontend, "async_panel_exists", return_value=True, create=True),
            patch.object(mod.frontend, "async_remove_panel", create=True) as remove,
        ):
            await mod.async_unload_entry(self.hass, None)
            remove.assert_called_once()
        original = {"devices": {}}
        store = mod.TunerStore()
        store.hass = types.SimpleNamespace(
            async_add_executor_job=AsyncMock(side_effect=lambda fn, *args: fn(*args))
        )
        self.assertEqual(await store._async_migrate_func(1, 0, original), original)

    async def test_panel_files_stay_those_of_the_running_backend_until_restart(self):
        """HACS rewrites the panel files before Home Assistant restarts; a reloaded panel
        then called room_series on the old backend and got "Unknown command."."""
        self.hass.data = {}
        self.hass.bus = types.SimpleNamespace(async_listen=Mock(return_value=Mock()))
        self.hass.http = types.SimpleNamespace(async_register_static_paths=AsyncMock())
        store = types.SimpleNamespace(
            async_load=AsyncMock(return_value={"devices": {}}), async_save=AsyncMock()
        )
        source = Path(self.hass.config.path("installed_static"))
        (source / "panel").mkdir(parents=True)
        (source / "ld2410-tuner-panel.js").write_text("running entrypoint")
        (source / "panel" / "room_graph.js").write_text("running room graph")
        with (
            patch.object(mod, "STATIC_PATH", source),
            patch.object(mod, "TunerStore", return_value=store),
            patch.object(
                mod, "StaticPathConfig", side_effect=lambda url, path, **kw: (url, path, kw)
            ),
            patch.object(mod, "_register_websocket_commands"),
            patch.object(mod.frontend, "async_panel_exists", return_value=False, create=True),
            patch.object(mod.frontend, "async_register_built_in_panel", create=True),
        ):
            await mod.async_setup_entry(self.hass, None)
            await self.initialization()
            # The update lands on disk while the old backend keeps running.
            (source / "ld2410-tuner-panel.js").write_text("updated entrypoint")
            (source / "panel" / "room_graph.js").write_text("updated room graph")
            await mod.async_unload_entry(self.hass, None)
            await mod.async_setup_entry(self.hass, None)
            await self.initialization()
            await mod.async_unload_entry(self.hass, None)
        [(paths,)] = [call.args for call in self.hass.http.async_register_static_paths.mock_calls]
        [(url, served, options)] = paths
        self.assertEqual(url, "/api/ld2410_tuner/static")
        self.assertEqual(options, {"cache_headers": False})
        served = Path(served)
        self.assertEqual((served / "ld2410-tuner-panel.js").read_text(), "running entrypoint")
        self.assertEqual((served / "panel" / "room_graph.js").read_text(), "running room graph")

    async def test_setup_does_not_wait_for_the_startup_history_cleanup(self):
        """Bootstrap stage 2 timed out while a first cleanup after the 1.24 upgrade thinned
        every converted day: setup commits the conversion and cleans in the background."""
        self.hass.data = {}
        self.hass.bus = types.SimpleNamespace(async_listen=Mock(return_value=Mock()))
        self.hass.http = types.SimpleNamespace(async_register_static_paths=AsyncMock())
        self.hass.async_create_background_task = lambda target, name: asyncio.create_task(target)
        store = types.SimpleNamespace(
            # A converted radar never cleaned yet: its first cleanup is due.
            async_load=AsyncMock(
                return_value={"devices": {"radar": {"reference_profile": {"version": 1}}}}
            ),
            async_save=AsyncMock(),
        )
        started, release = asyncio.Event(), asyncio.Event()

        async def slow_cleanup(runtime, *, persist=True):
            started.set()
            await release.wait()

        coordinator = sys.modules["tuner_under_test.runtime.coordinator"]
        with (
            patch.object(mod, "TunerStore", return_value=store),
            patch.object(mod, "StaticPathConfig", Mock()),
            patch.object(mod, "_register_websocket_commands"),
            patch.object(mod.frontend, "async_panel_exists", return_value=True, create=True),
            patch.object(coordinator.TunerRuntime, "async_clean_history", slow_cleanup),
            patch.object(mod.frontend, "async_remove_panel", create=True),
        ):
            self.assertTrue(await asyncio.wait_for(mod.async_setup_entry(self.hass, None), 5))
            await self.initialization()
            store.async_save.assert_awaited_once()  # The conversion is committed at once.
            await asyncio.wait_for(started.wait(), 5)  # The cleanup runs, after setup.
            runtime = self.hass.data[mod.DOMAIN]
            self.assertFalse(runtime._cleanup_task.done())
            release.set()
            await runtime._cleanup_task
            await mod.async_unload_entry(self.hass, None)

    def test_recording_appends_day_logs_and_saves_state_only_every_few_minutes(self):
        """Every 2 s tick asked for a full JSON save (a deep copy on the event loop and two
        serialisations of the store), saturating small machines. History needs only its day
        log appended; the recording state is saved every few minutes."""
        coordinator = sys.modules["tuner_under_test.runtime.coordinator"]
        self.add_entity("sensor.radar_g0_move_energy")
        self.registry.async_get = self.registry.entities.get
        self.runtime.refresh_devices(self.registry)
        saves, flushes = Mock(), Mock()
        self.runtime._schedule_save, self.runtime._schedule_flush = saves, flushes
        start = self.now
        for tick in range(300):  # Ten minutes of 2-second ticks.
            self.sample(start + 2 * tick, value=tick % 30)
        self.assertEqual(flushes.call_count, 300)
        self.assertEqual(saves.call_count, 598 // coordinator.STATE_SAVE_SECONDS)

    async def test_a_scheduled_flush_appends_recorded_changes_to_the_day_log(self):
        from tuner_under_test.history import dayfile
        from tuner_under_test.history.daystore import DayStore

        coordinator = sys.modules["tuner_under_test.runtime.coordinator"]
        del self.runtime._schedule_flush  # The harness stubs it; this is the real one.
        self.hass.async_create_background_task = lambda target, name: asyncio.create_task(target)
        self.hass.async_add_executor_job = lambda fn, *args: asyncio.to_thread(fn, *args)
        days = self.runtime._days = DayStore(Path(self.hass.config.path("history-flush")))
        days.append("a", dayfile.Change(self.now, {0: 12}))
        with patch.object(coordinator, "STORE_DELAY", 0):
            self.runtime._schedule_flush()
            first = self.runtime._flush_task
            self.runtime._schedule_flush()  # One pending flush is shared.
            self.assertIs(self.runtime._flush_task, first)
            await first
        self.assertTrue(list(days.folder("a").glob("*.log")))

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
        added = {"entities": {}}

        async def execute(request):
            if request["operation"] == "maintenance":
                self.runtime.data["devices"]["new"] = added

        self.worker_transport.after = execute
        await self.runtime.async_clean_history(persist=False)
        self.assertEqual(set(self.runtime.data["devices"]), {"a", "new"})
        self.assertNotIn("history_cleanup", added)
        self.assertIn("history_cleanup", self.device)
