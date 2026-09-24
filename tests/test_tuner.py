"""Run from the repository root with: python3 tests/test_tuner.py.

HA adapters are stubbed at the boundary; no live HA installation or radar required.
"""
import asyncio
import importlib.util
import sys
import time
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "ld2410_tuner"


def load_runtime():
    modules = ["voluptuous", "homeassistant", "homeassistant.components",
               "homeassistant.components.websocket_api", "homeassistant.components.frontend",
               "homeassistant.components.http", "homeassistant.config_entries",
               "homeassistant.core", "homeassistant.helpers", "homeassistant.helpers.entity_registry",
               "homeassistant.helpers.device_registry", "homeassistant.helpers.event",
               "homeassistant.helpers.storage"]
    for name in modules:
        sys.modules[name] = types.ModuleType(name)
    sys.modules["homeassistant.components.http"].StaticPathConfig = object
    sys.modules["homeassistant.config_entries"].ConfigEntry = object
    core = sys.modules["homeassistant.core"]
    core.HomeAssistant, core.callback = object, lambda fn: fn
    sys.modules["homeassistant.helpers.entity_registry"].EVENT_ENTITY_REGISTRY_UPDATED = "registry"
    events = sys.modules["homeassistant.helpers.event"]
    events.async_track_state_change_event = lambda *a: lambda: None
    events.async_track_time_interval = lambda *a: lambda: None
    sys.modules["homeassistant.helpers.storage"].Store = object
    spec = importlib.util.spec_from_file_location("tuner_under_test", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mod = load_runtime()
fit = sys.modules["tuner_under_test.learning"].fit_thresholds


def row_samples(present, negative, count=100):
    return [(i, dict(present), "present") for i in range(count)] + [(count+i, dict(negative), "not_present") for i in range(count)]


class LearningTests(unittest.TestCase):
    def test_weak_still_signal_is_not_sacrificed(self):
        result = fit(row_samples({"g0_still": 14}, {"g0_still": 10}), ["g0_still"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["proposals"]["g0_still"]["threshold"], 12)
        self.assertEqual(result["validation"]["sensitivity"], 1)

    def test_overlapping_classes_never_safe_at_zero_recall(self):
        result = fit(row_samples({"g0_move": 10}, {"g0_move": 10}), ["g0_move"])
        self.assertEqual(result["status"], "unsafe")
        self.assertEqual(result["validation"]["sensitivity"], 0)

    def test_equality_does_not_trigger(self):
        metrics = sys.modules["tuner_under_test.learning"].metrics
        result = metrics(row_samples({"g0_move": 100}, {"g0_move": 100}), {"g0_move": 100})
        self.assertEqual(result["sensitivity"], 0)
        self.assertEqual(result["false_positives"], 0)

    def test_different_locations_use_complementary_gates(self):
        rows = [(i, {"g0_move": 30 if i % 2 else 5, "g1_still": 5 if i % 2 else 20}, "present") for i in range(100)]
        rows += [(100+i, {"g0_move": 5, "g1_still": 5}, "not_present") for i in range(100)]
        result = fit(rows, ["g0_move", "g1_still"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["validation"]["sensitivity"], 1)
        self.assertTrue(all(p["sensitivity"] == .5 for p in result["proposals"].values()))

    def test_held_out_background_drift_blocks_apply(self):
        rows = row_samples({"g0_move": 30}, {"g0_move": 5})
        rows[-20:] = [(i, {"g0_move": 40}, "not_present") for i in range(180, 200)]
        result = fit(rows, ["g0_move"])
        self.assertEqual(result["training"]["false_positive_rate"], 0)
        self.assertEqual(result["validation"]["false_positive_rate"], 1)
        self.assertEqual(result["status"], "unsafe")

    def test_missing_gates_not_imputed_as_zero(self):
        result = fit(row_samples({"g0_move": 30}, {"g0_move": 5}), ["g0_move", "g1_move"])
        self.assertEqual(result["status"], "insufficient")

    def test_false_positive_budget_is_for_whole_device(self):
        # Each gate's noise spikes occur at different times; pooling per-gate
        # allowances would exceed the whole-device budget.
        keys = ["g0_move", "g1_move"]
        rows = [(i, {keys[0]: 30 if i % 2 else 5, keys[1]: 5 if i % 2 else 30}, "present") for i in range(1000)]
        negatives = [(1000+i, dict.fromkeys(keys, 5), "not_present") for i in range(1000)]
        for i in range(4): negatives[i][1][keys[0]] = 40
        for i in range(4, 8): negatives[i][1][keys[1]] = 40
        result = fit(rows+negatives, keys)
        self.assertLessEqual(result["training"]["false_positive_rate"], .005)
        self.assertEqual(result["status"], "unsafe")

    def test_guesses_cover_additional_location_at_lower_weight(self):
        keys = ["g0_move", "g1_still"]
        rows = row_samples({keys[0]: 30, keys[1]: 5}, dict.fromkeys(keys, 5))
        guesses = [(-100+i, {keys[0]: 5, keys[1]: 15}, "present", .6) for i in range(100)]
        manual_only = fit(rows, keys)
        assisted = fit(rows, keys, automatic=guesses)
        self.assertEqual(manual_only["proposals"][keys[1]]["threshold"], 100)
        self.assertLess(assisted["proposals"][keys[1]]["threshold"], 15)
        self.assertTrue(assisted["automatic_evidence"]["used"])
        self.assertAlmostEqual(assisted["automatic_evidence"]["effective_weight"]["present"], 12)
        self.assertEqual(assisted["validation"], manual_only["validation"])

    def test_confidence_changes_effective_weight_and_volume_is_capped(self):
        rows = row_samples({"g0_move": 30}, {"g0_move": 5})
        def weighted(count, confidence):
            guesses = [(-count+i, {"g0_move": 25}, "present", confidence) for i in range(count)]
            return fit(rows, ["g0_move"], automatic=guesses)["automatic_evidence"]["effective_weight"]["present"]
        self.assertGreater(weighted(50, .9), weighted(50, .6))
        self.assertAlmostEqual(weighted(1000, .99), 20)

    def test_automatic_only_recommendation_is_provisional(self):
        guesses = [(i, {"g0_move": 30 if i < 100 else 5}, "present" if i < 100 else "not_present", .65) for i in range(200)]
        result = fit([], ["g0_move"], automatic=guesses)
        self.assertEqual(result["status"], "provisional")
        self.assertIsNone(result["validation"])
        self.assertLess(result["proposals"]["g0_move"]["threshold"], 30)

    def test_guesses_after_validation_begins_are_deferred(self):
        rows = row_samples({"g0_move": 30}, {"g0_move": 5})
        result = fit(rows, ["g0_move"], automatic=[(300, {"g0_move": 50}, "not_present", .99)])
        self.assertEqual(result["automatic_evidence"]["deferred_samples"], 1)
        self.assertFalse(result["automatic_evidence"]["used"])

    def test_one_missed_validation_sample_is_not_almost_perfect(self):
        rows = row_samples({"g0_move": 30}, {"g0_move": 5})
        rows[99] = (99, {"g0_move": 5}, "present")
        result = fit(rows, ["g0_move"])
        self.assertEqual(result["validation"]["sensitivity"], .95)
        self.assertEqual(result["status"], "unsafe")
        self.assertEqual(result["targets"]["sensitivity"], .999)

    def test_episode_and_burst_metrics_reveal_temporal_failures(self):
        metrics = sys.modules["tuner_under_test.learning"].metrics
        rows = [(0, {"g0_move": 30}, "present"), (6, {"g0_move": 30}, "present"),
                (30, {"g0_move": 5}, "present"), (36, {"g0_move": 5}, "present"),
                (60, {"g0_move": 30}, "not_present"), (66, {"g0_move": 30}, "not_present"),
                (72, {"g0_move": 5}, "not_present"), (78, {"g0_move": 30}, "not_present")]
        result = metrics(rows, {"g0_move": 10})
        self.assertEqual(result["presence_episodes"], 2)
        self.assertEqual(result["missed_presence_episodes"], 1)
        self.assertEqual(result["longest_missed_run_samples"], 2)
        self.assertEqual(result["false_trigger_bursts"], 2)

    def test_near_perfect_average_cannot_hide_entire_missed_episode(self):
        rows = [(i*6, {"g0_move": 30}, "present") for i in range(5000)]
        rows[-1] = (30100, {"g0_move": 5}, "present")
        rows += [(40000+i*6, {"g0_move": 5}, "not_present") for i in range(5000)]
        result = fit(rows, ["g0_move"])
        self.assertEqual(result["validation"]["sensitivity"], .999)
        self.assertEqual(result["validation"]["missed_presence_episodes"], 1)
        self.assertEqual(result["status"], "unsafe")

    def test_low_false_positive_percentage_cannot_hide_frequent_bursts(self):
        rows = [(i*6, {"g0_move": 30}, "present") for i in range(5000)]
        rows += [(40000+i*6, {"g0_move": 40 if i in (4100, 4400, 4800) else 5}, "not_present") for i in range(5000)]
        result = fit(rows, ["g0_move"])
        self.assertLessEqual(result["validation"]["false_positive_rate"], .005)
        self.assertGreater(result["validation"]["false_trigger_bursts_per_hour"], 1)
        self.assertEqual(result["status"], "unsafe")



class InferenceTests(unittest.TestCase):
    def histogram(self, value):
        hist = [0]*101
        hist[value] = 100
        return hist

    def test_weak_stationary_signal_accumulates_over_time(self):
        infer = sys.modules["tuner_under_test.inference"].estimate_presence
        manual = {"g0_still": {"present": self.histogram(14), "not_present": self.histogram(10)}}
        temporal = {}
        results = [infer({"g0_still": 13}, manual, {}, temporal, 100+i*2, ["g0_still"]) for i in range(8)]
        self.assertEqual(results[-1]["label"], "present")
        self.assertGreater(results[-1]["confidence"], .9)
        self.assertGreater(results[-1]["presence_probability"], results[0]["presence_probability"])

    def test_move_and_still_from_same_gate_not_double_counted(self):
        infer = sys.modules["tuner_under_test.inference"].estimate_presence
        manual = {key: {"present": self.histogram(20), "not_present": self.histogram(5)} for key in ("g0_move", "g0_still")}
        single = infer({"g0_move": 20}, manual, {}, {}, 100, ["g0_move"])
        paired = infer({"g0_move": 20, "g0_still": 20}, manual, {}, {}, 100, list(manual))
        self.assertEqual(single["score"], paired["score"])

    def test_missing_gate_is_unknown_not_absent(self):
        infer = sys.modules["tuner_under_test.inference"].estimate_presence
        result = infer({"g0_move": 5}, {}, {"g0_move": self.histogram(5)}, {}, 100, ["g0_move", "g1_move"])
        self.assertEqual(result["label"], "unknown")

    def test_uninformative_channels_do_not_hide_learned_absence(self):
        infer = sys.modules["tuner_under_test.inference"].estimate_presence
        manual = {"g0_still": {"present": self.histogram(20), "not_present": self.histogram(5)},
                  "g0_move": {"present": self.histogram(5), "not_present": self.histogram(5)},
                  "g1_move": {"present": self.histogram(5), "not_present": self.histogram(5)}}
        temporal = {"probability": .99, "timestamp": 100}
        for i in range(8):
            result = infer(dict.fromkeys(manual, 5), manual, {}, temporal, 100+i*2, list(manual))
        self.assertEqual(result["label"], "not_present")
        self.assertGreater(result["confidence"], .9)

    def test_bootstrap_confidence_is_capped(self):
        infer = sys.modules["tuner_under_test.inference"].estimate_presence
        temporal = {}
        result = None
        for i in range(10):
            result = infer({"g0_move": 30}, {}, {"g0_move": self.histogram(5)}, temporal, 100+i*2, ["g0_move"])
        self.assertEqual(result["label"], "present")
        self.assertEqual(result["confidence"], .65)



class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.states = {}
        self.registry = types.SimpleNamespace(entities={})
        mod.er.async_get = lambda hass: self.registry
        self.hass = types.SimpleNamespace(async_add_executor_job=lambda fn,*args: asyncio.to_thread(fn,*args), states=types.SimpleNamespace(get=self.states.get), services=types.SimpleNamespace(async_call=AsyncMock()), async_create_task=asyncio.create_task)
        self.device = {"entities": {"sensor.radar_g0_move_energy": {"gate": 0, "kind": "move"}}, "training_state": "unknown"}
        self.runtime = mod.TunerRuntime(self.hass, types.SimpleNamespace(async_save=AsyncMock()), {"devices": {"a": self.device}})
        self.runtime._schedule_save = lambda: None
        self.now = time.time()

    async def asyncTearDown(self):
        tasks = list(self.runtime._timeout_tasks.values())
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def sample(self, timestamp, value=10):
        self.states["sensor.radar_g0_move_energy"] = types.SimpleNamespace(state=str(value))
        with patch.object(mod.time, "time", return_value=timestamp):
            self.runtime.sample_devices()

    def test_constant_signal_is_sampled_without_state_events(self):
        self.device.update(training_state="present", training_label_start=self.now-1)
        for i in range(3): self.sample(self.now+i*6)
        self.assertEqual(sum(self.device["histograms"]["g0_move"]["present"]), 3)

    def test_unavailable_reading_is_not_reused(self):
        self.sample(self.now)
        self.sample(self.now+6, "unavailable")
        self.assertEqual(self.runtime._live["a"], {})
        self.assertEqual(len(list(self.runtime._iter_history_samples(self.device))), 1)

    def test_buffered_history_visible_immediately(self):
        self.sample(self.now-2)
        result = self.runtime.history_series_multi("a", ["g0_move"], 1)
        self.assertEqual(result["series"]["g0_move"]["sample_count"], 1)
        self.assertEqual(self.device.get("history", []), [])

    def test_relabel_is_idempotent_and_does_not_duplicate_initial_training(self):
        self.device.update(training_state="present", training_label_start=self.now-20)
        self.sample(self.now-12)
        self.sample(self.now-6)
        for _ in range(2): self.runtime.label_history_range("a", self.now-15, self.now-1, "not_present")
        hist = self.device["histograms"]["g0_move"]
        self.assertEqual(sum(hist["present"]), 0)
        self.assertEqual(sum(hist["not_present"]), 2)

    def test_unknown_clears_only_requested_half_open_range(self):
        self.device.update(training_state="present", training_label_start=self.now-20)
        self.sample(self.now-12)
        self.sample(self.now-6)
        self.runtime.label_history_range("a", self.now-12, self.now-6, "unknown")
        hist = self.device["histograms"]["g0_move"]
        self.assertEqual(sum(hist["present"]), 1)

    def test_empty_auto_training_is_unknown(self):
        result = self.runtime._classify_auto(self.device, {"g0_move": 50})
        self.assertEqual(result["label"], "unknown")

    def test_clear_invalidates_learned_and_stops_training(self):
        self.device.update(training_state="present", training_label_start=self.now-20, last_learning={"status": "ok"})
        self.sample(self.now-6)
        self.runtime.clear_samples("a")
        self.assertNotIn("last_learning", self.device)
        self.assertEqual(self.device["training_state"], "unknown")
        self.assertEqual(list(self.runtime._iter_history_samples(self.device)), [])

    def test_unknown_device_training_rejected(self):
        with self.assertRaises(ValueError): self.runtime.set_training_state("bogus", "present")
        self.assertNotIn("bogus", self.runtime.data["devices"])

    def test_long_history_gap_does_not_overflow_offset(self):
        self.runtime._record_history_sample("a", {"g0_move": 10}, self.now-70000)
        self.runtime._record_history_sample("a", {"g0_move": 20}, self.now)
        timestamps = [ts for ts, _ in self.runtime._iter_history_samples(self.device)]
        self.assertEqual(timestamps, [self.now-70000, self.now])

    async def test_cancelled_timeout_cannot_remove_replacement(self):
        self.runtime.set_training_state("a", "present", 100)
        await asyncio.sleep(0)
        self.runtime.set_training_state("a", "not_present", 200)
        new_task = self.runtime._timeout_tasks["a"]
        await asyncio.sleep(0)
        self.assertIs(self.runtime._timeout_tasks["a"], new_task)

    def test_expired_training_does_not_label_restart_gap(self):
        self.device.update(training_state="present", training_label_start=self.now-30, training_expires_at=self.now-20)
        self.runtime._close_training_interval(self.device, self.now)
        self.assertEqual(self.device["history_labels"][0]["end"], self.now-20)

    def configuration(self):
        for i in range(3):
            entity_id = f"number.radar_g{i}_move_threshold"
            self.registry.entities[entity_id] = types.SimpleNamespace(device_id="a", domain="number", entity_id=entity_id)
            self.states[entity_id] = types.SimpleNamespace(state="10")
        for i in range(3):
            still_id = f"number.radar_g{i}_still_threshold"
            self.registry.entities[still_id] = types.SimpleNamespace(device_id="a", domain="number", entity_id=still_id)
            self.states[still_id] = types.SimpleNamespace(state="10")
        for kind, limit in (("move", 2), ("still", 2)):
            entity_id = f"number.radar_max_{kind}_distance_gate"
            self.registry.entities[entity_id] = types.SimpleNamespace(device_id="a", domain="number", entity_id=entity_id)
            self.states[entity_id] = types.SimpleNamespace(state=str(limit))
        entities, current = self.runtime._threshold_configuration("a")
        self.device["last_learning"] = {"method": "joint_temporal_v2", "status": "ok", "entities": entities, "configuration": current, "proposals": {key: {"threshold": 20} for key in entities}}

    async def test_apply_stops_after_partial_failure(self):
        self.configuration()
        self.hass.services.async_call.side_effect = [None, RuntimeError("offline")]
        result = await self.runtime.apply("a")
        self.assertEqual(len(result["applied"]), 1)
        self.assertEqual(len(result["skipped"]), 5)
        self.assertEqual(self.hass.services.async_call.await_count, 2)

    async def test_apply_requires_preview_and_unchanged_configuration(self):
        with self.assertRaises(ValueError): await self.runtime.apply("a")
        self.configuration()
        self.states["number.radar_g0_move_threshold"].state = "30"
        with self.assertRaises(ValueError): await self.runtime.apply("a")
        self.hass.services.async_call.assert_not_awaited()

    def test_nonfinite_history_range_rejected(self):
        with self.assertRaises(ValueError): self.runtime.label_history_range("a", float("nan"), self.now, "present")
        with self.assertRaises(ValueError): self.runtime.history_series_multi("a", ["g0_move"], float("inf"))

    def test_unmanaged_gate_blocks_device_wide_learning(self):
        self.configuration()
        self.registry.entities.pop("number.radar_g1_move_threshold")
        with self.assertRaisesRegex(ValueError, "Enable all active"):
            self.runtime._threshold_configuration("a")

    async def test_chart_executor_includes_pending_data(self):
        self.sample(self.now-2)
        result = await self.runtime.async_history_series("a", ["g0_move"], 1, 400)
        self.assertEqual(result["series"]["g0_move"]["sample_count"], 1)

    async def test_learning_executor_does_not_use_unlabelled_auto_samples(self):
        self.configuration()
        result = await self.runtime.async_learn("a")
        self.assertEqual(result["status"], "insufficient")
        self.assertEqual(result["counts"], {"present": 0, "not_present": 0})

    async def test_complete_runtime_learning_and_apply(self):
        self.configuration()
        keys = list(self.device["last_learning"]["entities"])
        samples = []
        start = self.now - 1200
        for i in range(200):
            values = {key: 5 for key in keys}
            if i < 100:
                values["g0_move" if i % 2 else "g2_still"] = 30
            row = bytes(values.get(key, 255) for key in mod.HISTORY_KEYS)
            samples.append((start + i * 6, row))
        self.runtime._history_runtime["a"] = {"samples": samples}
        self.device["history_labels"] = [
            {"start": start, "end": start+600, "state": "present"},
            {"start": start+600, "end": self.now, "state": "not_present"},
        ]
        learned = await self.runtime.async_learn("a")
        self.assertEqual(learned["status"], "ok")
        self.assertEqual(learned["validation"]["sensitivity"], 1)
        result = await self.runtime.apply("a")
        self.assertEqual(set(result["applied"]), set(keys))
        self.assertEqual(result["skipped"], {})

    async def test_changed_labels_discard_inflight_learning(self):
        self.configuration()
        async def execute(fn, *args):
            self.runtime.clear_samples("a")
            return fn(*args)
        self.hass.async_add_executor_job = execute
        with self.assertRaisesRegex(ValueError, "labels changed"):
            await self.runtime.async_learn("a")
        self.assertNotIn("last_learning", self.device)

    async def test_concurrent_apply_is_rejected(self):
        self.configuration()
        entered, release = asyncio.Event(), asyncio.Event()
        async def write(*args, **kwargs):
            entered.set()
            await release.wait()
        self.hass.services.async_call.side_effect = write
        first = asyncio.create_task(self.runtime.apply("a"))
        await entered.wait()
        with self.assertRaisesRegex(ValueError, "already in progress"):
            await self.runtime.apply("a")
        release.set()
        await first
        self.assertEqual(self.runtime._applying, set())

    def test_bootstrap_guesses_without_human_labels(self):
        for i in range(45): self.sample(self.now+i*2, 5)
        self.assertEqual(self.device["auto"]["last_classification"]["state"], "not_present")
        for i in range(45, 55): self.sample(self.now+i*2, 30)
        last = self.device["auto"]["last_classification"]
        self.assertEqual(last["state"], "present")
        self.assertEqual(last["confidence"], .65)
        self.assertEqual(last["basis"], "bootstrap")

    def test_short_spike_cannot_confirm_entry_from_filter_memory(self):
        for i in range(45): self.sample(self.now+i*2, 5)
        states = []
        for i in range(45, 65):
            self.sample(self.now+i*2, 30 if i < 47 else 5)
            states.append(self.device["auto"]["last_classification"]["state"])
        self.assertNotIn("present", states)

    def test_confidence_survives_history_flush_and_old_blocks_still_read(self):
        self.device["auto"] = {"last_classification": {"state": "present", "confidence": .73, "timestamp": self.now}}
        self.runtime._record_history_sample("a", {"g0_move": 20}, self.now)
        self.runtime._flush_history_block("a")
        self.assertEqual(self.device["history"][0]["version"], 2)
        row = list(self.runtime._iter_history_samples(self.device, include_auto=True))[0][1]
        self.assertEqual(tuple(row[-2:]), (1, 73))
        self.assertEqual(len(list(self.runtime._iter_history_samples(self.device))[0][1]), 18)
        old_raw = mod.struct.pack(">H", 0) + bytes([5]*18)
        self.device["history"].append({"start": self.now+6, "count": 1, "data": mod.base64.b64encode(mod.zlib.compress(old_raw)).decode()})
        rows = list(self.runtime._iter_history_samples(self.device, include_auto=True))
        self.assertEqual(len(rows[-1][1]), 18)

    def test_manual_corrections_and_explicit_unknown_override_guesses(self):
        self.configuration()
        keys = list(self.device["last_learning"]["entities"])
        values = bytes(30 if key in keys else 255 for key in mod.HISTORY_KEYS)
        self.runtime._history_runtime["a"] = {"samples": [(self.now-12, values+bytes((1, 90))), (self.now-6, values+bytes((1, 90)))]}
        self.runtime.label_history_range("a", self.now-15, self.now-9, "not_present")
        self.runtime.label_history_range("a", self.now-9, self.now-1, "unknown")
        entities, current = self.runtime._threshold_configuration("a")
        result = self.runtime._fit_history("a", entities, current)
        self.assertEqual(result["counts"]["not_present"], 1)
        self.assertEqual(result["automatic_evidence"]["samples"]["present"], 0)

    async def test_persisted_guesses_reach_learner_with_confidence(self):
        self.configuration()
        keys = list(self.device["last_learning"]["entities"])
        samples = []
        for i in range(200):
            values = {key: 5 for key in keys}
            if i < 100: values["g0_move"] = 30
            row = bytes(values.get(key, 255) for key in mod.HISTORY_KEYS)
            samples.append((self.now-1200+i*6, row+bytes((1 if i < 100 else 2, 65))))
        self.runtime._history_runtime["a"] = {"samples": samples}
        self.runtime._flush_history_block("a")
        result = await self.runtime.async_learn("a")
        self.assertEqual(result["status"], "provisional")
        self.assertEqual(result["automatic_evidence"]["samples"], {"present": 100, "not_present": 100})
        self.assertAlmostEqual(result["automatic_evidence"]["mean_confidence"]["present"], .65)
        with self.assertRaises(ValueError): await self.runtime.apply("a")

    async def test_previous_95_percent_model_cannot_be_applied(self):
        self.configuration()
        self.device["last_learning"]["method"] = "joint_labelled_v1"
        with self.assertRaises(ValueError): await self.runtime.apply("a")
        self.hass.services.async_call.assert_not_awaited()

    async def test_unload_flushes_partial_history(self):
        self.sample(self.now-2)
        self.hass.data = {mod.DOMAIN: self.runtime}
        mod.frontend.async_panel_exists = lambda *a: False
        await mod.async_unload_entry(self.hass, None)
        self.assertEqual(self.device["history"][0]["count"], 1)
        self.runtime.store.async_save.assert_awaited_once()


if __name__ == "__main__": unittest.main(verbosity=2)
