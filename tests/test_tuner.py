"""Run from the repository root with: python3 tests/test_tuner.py.

HA adapters are stubbed at the boundary; no live HA installation or radar required.
"""

import asyncio
import base64
import importlib.util
import struct
import sys
import time
import types
import unittest
import zlib
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "ld2410_tuner"


def load_runtime():
    modules = [
        "voluptuous",
        "homeassistant",
        "homeassistant.components",
        "homeassistant.components.websocket_api",
        "homeassistant.components.frontend",
        "homeassistant.components.http",
        "homeassistant.config_entries",
        "homeassistant.core",
        "homeassistant.helpers",
        "homeassistant.helpers.entity_registry",
        "homeassistant.helpers.device_registry",
        "homeassistant.helpers.event",
        "homeassistant.helpers.storage",
    ]
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
    spec = importlib.util.spec_from_file_location(
        "tuner_under_test", ROOT / "__init__.py", submodule_search_locations=[str(ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


mod = load_runtime()
constants = sys.modules["tuner_under_test.const"]
training = sys.modules["tuner_under_test.history.labels"]
ws = sys.modules["tuner_under_test.runtime.websocket"]
fit = sys.modules["tuner_under_test.calibration.fitting"].fit_thresholds


def row_samples(present, negative, count=100):
    return [(i, dict(present), "present") for i in range(count)] + [
        (count + i, dict(negative), "not_present") for i in range(count)
    ]


class LearningTests(unittest.TestCase):
    def test_isolated_miss_within_targets_does_not_make_device_always_on(self):
        keys = ["g1_move", "g3_still"]
        rows = [
            (i * 6, {keys[0]: 7, keys[1]: 4 if i == 100 else 40}, "present") for i in range(2868)
        ]
        rows += [
            (30000 + i * 6, {keys[0]: 9 if i < 4316 else 4, keys[1]: 4}, "not_present")
            for i in range(5000)
        ]
        result = fit(rows, keys)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["training"]["false_negatives"], 1)
        self.assertEqual(result["training"]["false_positives"], 0)
        self.assertGreaterEqual(result["training"]["sensitivity"], 0.999)
        self.assertEqual(result["training"]["missed_presence_episodes"], 0)
        self.assertEqual(result["training"]["longest_missed_run_samples"], 1)
        self.assertEqual(result["recent_training"]["false_negatives"], 0)

    def test_recent_presence_target_is_part_of_search(self):
        keys = ["g1_move", "g3_still"]
        rows = [
            (i * 6, {keys[0]: 7, keys[1]: 4 if i == 2800 else 40}, "present") for i in range(2868)
        ]
        rows += [(30000 + i * 6, {keys[0]: 9, keys[1]: 4}, "not_present") for i in range(5000)]
        result = fit(rows, keys)
        self.assertEqual(result["status"], "unsafe")
        self.assertEqual(result["recent_training"]["false_negatives"], 0)

    def test_small_miss_budget_does_not_allow_consecutive_misses(self):
        keys = ["g1_move", "g3_still"]
        rows = [
            (i * 6, {keys[0]: 7, keys[1]: 4 if i in (100, 101) else 40}, "present")
            for i in range(5000)
        ]
        rows += [(40000 + i * 6, {keys[0]: 9, keys[1]: 4}, "not_present") for i in range(5000)]
        result = fit(rows, keys)
        self.assertEqual(result["status"], "unsafe")
        self.assertLessEqual(result["training"]["longest_missed_run_samples"], 1)

    def test_overlapping_noisy_gates_do_not_trap_coordinate_search(self):
        keys = ["g0_move", "g1_move", "g2_still"]
        rows = [(i * 6, dict.fromkeys(keys, 30), "present") for i in range(1000)]
        rows += [
            (
                10000 + i * 6,
                {keys[0]: 20 if i < 6 else 10, keys[1]: 20 if i < 6 else 10, keys[2]: 10},
                "not_present",
            )
            for i in range(1000)
        ]
        result = fit(rows, keys)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["training"]["false_positives"], 0)
        self.assertEqual(result["training"]["false_negatives"], 0)
        self.assertTrue(all(p["threshold"] < 100 for p in result["proposals"].values()))

    def test_fast_search_constraints_match_public_validation(self):
        import random

        learning = sys.modules["tuner_under_test.calibration.fitting"]
        rng = random.Random(42)
        for _case in range(30):
            timestamp = 0
            rows = []
            for i in range(160):
                timestamp += rng.choice([1, 6, 6, 20])
                rows.append(
                    (
                        timestamp,
                        {"g0_move": rng.randrange(31)},
                        "present" if (i // 20) % 2 else "not_present",
                    )
                )
            positive = [row for row in rows if row[2] == "present"]
            negative = [row for row in rows if row[2] == "not_present"]
            threshold = rng.randrange(31)
            detected = learning._masks(positive, "g0_move")[threshold]
            false = learning._masks(negative, "g0_move")[threshold]
            score = learning._human_ranker(positive, negative)(detected, false)
            all_metrics = learning.metrics(positive + negative, {"g0_move": threshold})
            recent = learning.metrics(
                positive[int(0.8 * len(positive)) :] + negative[int(0.8 * len(negative)) :],
                {"g0_move": threshold},
            )
            failures = learning._human_failures(all_metrics) + learning._human_failures(recent)
            self.assertEqual(any(score[:5]), bool(failures))
            self.assertEqual(
                score[5:],
                (
                    all_metrics["false_negatives"],
                    all_metrics["false_positives"],
                    all_metrics["false_trigger_bursts"],
                ),
            )

    def test_redundant_useful_gates_are_not_disabled(self):
        keys = list(constants.HISTORY_KEYS)
        result = fit(row_samples(dict.fromkeys(keys, 20), dict.fromkeys(keys, 10)), keys)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(all(p["threshold"] == 12 for p in result["proposals"].values()))

    def test_many_guesses_cannot_overrule_a_few_human_labels(self):
        rows = [(0, {"g0_still": 14}, "present"), (6, {"g0_still": 10}, "not_present")]
        guesses = [
            (
                12 + i * 6,
                {"g0_still": 10 if i % 2 else 14},
                "present" if i % 2 else "not_present",
                0.99,
            )
            for i in range(10000)
        ]
        result = fit(rows, ["g0_still"], automatic=guesses)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["training"]["sensitivity"], 1)
        self.assertEqual(result["training"]["false_positive_rate"], 0)
        self.assertTrue(result["automatic_evidence"]["used"])
        self.assertIsNone(result["validation"])

    def test_refit_learns_a_location_first_seen_in_recent_labels(self):
        rows = row_samples({"g0_move": 30, "g1_still": 5}, {"g0_move": 5, "g1_still": 5})
        rows[99] = (99, {"g0_move": 5, "g1_still": 6}, "present")
        result = fit(rows, ["g0_move", "g1_still"])
        self.assertEqual(result["validation"]["false_negatives"], 1)
        self.assertEqual(result["training"]["false_negatives"], 0)
        self.assertEqual(result["status"], "ok")
        self.assertLess(result["proposals"]["g1_still"]["threshold"], 6)

    def test_partial_gate_observations_still_contribute(self):
        rows = [(i * 6, {"g0_move" if i % 2 else "g1_still": 20}, "present") for i in range(100)]
        rows += [
            (600 + i * 6, {"g0_move" if i % 2 else "g1_still": 5}, "not_present")
            for i in range(100)
        ]
        result = fit(rows, ["g0_move", "g1_still"])
        self.assertEqual(result["counts"], {"present": 100, "not_present": 100})
        self.assertEqual(result["status"], "ok")

    def test_weak_still_signal_is_not_sacrificed(self):
        result = fit(row_samples({"g0_still": 14}, {"g0_still": 10}), ["g0_still"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["proposals"]["g0_still"]["threshold"], 12)
        self.assertEqual(result["validation"]["sensitivity"], 1)

    def test_overlapping_classes_never_safe_at_zero_recall(self):
        result = fit(row_samples({"g0_move": 10}, {"g0_move": 10}), ["g0_move"])
        self.assertEqual(result["status"], "unsafe")
        self.assertEqual(result["training"]["false_positive_rate"], 1)

    def test_equality_does_not_trigger(self):
        metrics = sys.modules["tuner_under_test.calibration.fitting"].metrics
        result = metrics(row_samples({"g0_move": 100}, {"g0_move": 100}), {"g0_move": 100})
        self.assertEqual(result["sensitivity"], 0)
        self.assertEqual(result["false_positives"], 0)

    def test_different_locations_use_complementary_gates(self):
        rows = [
            (i, {"g0_move": 30 if i % 2 else 5, "g1_still": 5 if i % 2 else 20}, "present")
            for i in range(100)
        ]
        rows += [(100 + i, {"g0_move": 5, "g1_still": 5}, "not_present") for i in range(100)]
        result = fit(rows, ["g0_move", "g1_still"])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["validation"]["sensitivity"], 1)
        self.assertTrue(all(p["sensitivity"] == 0.5 for p in result["proposals"].values()))

    def test_held_out_background_drift_blocks_apply(self):
        rows = row_samples({"g0_move": 30}, {"g0_move": 5})
        rows[-20:] = [(i, {"g0_move": 40}, "not_present") for i in range(180, 200)]
        result = fit(rows, ["g0_move"])
        self.assertEqual(result["training"]["false_positive_rate"], 0.2)
        self.assertEqual(result["validation"]["false_positive_rate"], 1)
        self.assertEqual(result["status"], "unsafe")

    def test_missing_gates_not_imputed_as_zero(self):
        result = fit(
            row_samples({"g0_move": 30}, {"g0_move": 5}),
            ["g0_move", "g1_move"],
            current={"g0_move": 20, "g1_move": 42},
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["proposals"]["g1_move"]["threshold"], 42)
        self.assertEqual(result["proposals"]["g1_move"]["role"], "unchanged")

    def test_false_positive_budget_is_for_whole_device(self):
        # Each gate's noise spikes occur at different times; pooling per-gate
        # allowances would exceed the whole-device budget.
        keys = ["g0_move", "g1_move"]
        rows = [
            (i, {keys[0]: 30 if i % 2 else 5, keys[1]: 5 if i % 2 else 30}, "present")
            for i in range(1000)
        ]
        negatives = [(1000 + i, dict.fromkeys(keys, 5), "not_present") for i in range(1000)]
        for i in range(4):
            negatives[i][1][keys[0]] = 40
        for i in range(4, 8):
            negatives[i][1][keys[1]] = 40
        result = fit(rows + negatives, keys)
        self.assertEqual(result["training"]["false_positive_rate"], 0.008)
        self.assertEqual(result["status"], "unsafe")
        self.assertEqual(result["training"]["false_positives"], 8)
        for proposal in result["proposals"].values():
            self.assertEqual(proposal["false_positives"], 4)
            self.assertEqual(proposal["not_present_samples"], 1000)
            self.assertIn("8 false triggers in 1000", proposal["message"])

    def test_guesses_cover_additional_location_at_lower_weight(self):
        keys = ["g0_move", "g1_still"]
        rows = row_samples({keys[0]: 30, keys[1]: 5}, dict.fromkeys(keys, 5))
        guesses = [(-100 + i, {keys[0]: 5, keys[1]: 6}, "present", 0.6) for i in range(100)]
        manual_only = fit(rows, keys)
        assisted = fit(rows, keys, automatic=guesses)
        self.assertEqual(manual_only["proposals"][keys[1]]["threshold"], 7)
        self.assertLess(assisted["proposals"][keys[1]]["threshold"], 6)
        self.assertTrue(assisted["automatic_evidence"]["used"])
        self.assertAlmostEqual(assisted["automatic_evidence"]["effective_weight"]["present"], 12)
        self.assertEqual(assisted["validation"], manual_only["validation"])

    def test_confidence_changes_effective_weight_and_volume_is_capped(self):
        rows = row_samples({"g0_move": 30}, {"g0_move": 5})

        def weighted(count, confidence):
            guesses = [(-count + i, {"g0_move": 25}, "present", confidence) for i in range(count)]
            return fit(rows, ["g0_move"], automatic=guesses)["automatic_evidence"][
                "effective_weight"
            ]["present"]

        self.assertGreater(weighted(50, 0.9), weighted(50, 0.6))
        self.assertAlmostEqual(weighted(1000, 0.99), 25)

    def test_automatic_only_recommendation_is_usable_but_identified_as_estimated(self):
        guesses = [
            (i, {"g0_move": 30 if i < 100 else 5}, "present" if i < 100 else "not_present", 0.65)
            for i in range(200)
        ]
        result = fit([], ["g0_move"], automatic=guesses)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["evidence_basis"], "automatic")
        self.assertIsNone(result["validation"])
        self.assertLess(result["proposals"]["g0_move"]["threshold"], 30)

    def test_newer_guesses_are_used_by_final_fit_without_changing_backtest(self):
        rows = row_samples({"g0_move": 30}, {"g0_move": 5})
        result = fit(rows, ["g0_move"], automatic=[(300, {"g0_move": 50}, "not_present", 0.99)])
        self.assertEqual(result["automatic_evidence"]["deferred_samples"], 0)
        self.assertTrue(result["automatic_evidence"]["used"])
        self.assertEqual(result["validation"], fit(rows, ["g0_move"])["validation"])

    def test_one_missed_validation_sample_is_not_almost_perfect(self):
        rows = row_samples({"g0_move": 30}, {"g0_move": 5})
        rows[99] = (99, {"g0_move": 5}, "present")
        result = fit(rows, ["g0_move"])
        self.assertEqual(result["validation"]["sensitivity"], 0.95)
        self.assertEqual(result["status"], "unsafe")
        self.assertEqual(result["targets"]["sensitivity"], 0.999)

    def test_episode_and_burst_metrics_reveal_temporal_failures(self):
        metrics = sys.modules["tuner_under_test.calibration.fitting"].metrics
        rows = [
            (0, {"g0_move": 30}, "present"),
            (6, {"g0_move": 30}, "present"),
            (30, {"g0_move": 5}, "present"),
            (36, {"g0_move": 5}, "present"),
            (60, {"g0_move": 30}, "not_present"),
            (66, {"g0_move": 30}, "not_present"),
            (72, {"g0_move": 5}, "not_present"),
            (78, {"g0_move": 30}, "not_present"),
        ]
        result = metrics(rows, {"g0_move": 10})
        self.assertEqual(result["presence_episodes"], 2)
        self.assertEqual(result["missed_presence_episodes"], 1)
        self.assertEqual(result["longest_missed_run_samples"], 2)
        self.assertEqual(result["false_trigger_bursts"], 2)

    def test_near_perfect_average_cannot_hide_entire_missed_episode(self):
        rows = [(i * 6, {"g0_move": 30}, "present") for i in range(5000)]
        rows[-1] = (30100, {"g0_move": 5}, "present")
        rows += [(40000 + i * 6, {"g0_move": 5}, "not_present") for i in range(5000)]
        result = fit(rows, ["g0_move"])
        self.assertEqual(result["validation"]["sensitivity"], 0.999)
        self.assertEqual(result["validation"]["missed_presence_episodes"], 1)
        self.assertEqual(result["status"], "unsafe")

    def test_low_false_positive_percentage_cannot_hide_frequent_bursts(self):
        rows = [(i * 6, {"g0_move": 30}, "present") for i in range(5000)]
        rows += [
            (40000 + i * 6, {"g0_move": 40 if i in (4100, 4400, 4800) else 5}, "not_present")
            for i in range(5000)
        ]
        result = fit(rows, ["g0_move"])
        self.assertLessEqual(result["validation"]["false_positive_rate"], 0.005)
        self.assertGreater(result["validation"]["false_trigger_bursts_per_hour"], 1)
        self.assertEqual(result["status"], "unsafe")


class HistoryCleanupTests(unittest.TestCase):
    def block(self, start, samples, version=1):
        import base64
        import struct
        import zlib

        raw = b"".join(struct.pack(">H", offset) + bytes(values) for offset, values in samples)
        return {
            "start": start,
            "end": start + max(offset for offset, values in samples),
            "version": version,
            "count": len(samples),
            "data": base64.b64encode(zlib.compress(raw)).decode(),
        }

    def test_migration_preserves_timestamps_and_unknown_confidence(self):
        from tuner_under_test.history.cleanup import clean_history

        first = self.block(100.25, [(0, [5] * 18), (6, [7] * 18)])
        second = self.block(112.75, [(0, [9] * 18 + [1, 80])], 2)
        device = {
            "history": [first, second],
            "history_labels": [{"start": 99, "end": 120, "state": "present"}],
        }
        updated, stats = clean_history(device, [], 130, 1000)
        runtime = mod.TunerRuntime(None, None, {"devices": {"a": updated}})
        with patch.object(time, "time", return_value=130):
            rows = list(runtime._iter_history_samples(updated, include_auto=True))
        self.assertEqual([ts for ts, row in rows], [100.25, 106.25, 112.75])
        self.assertEqual([row[-2:] for ts, row in rows], [bytes(2), bytes(2), bytes([1, 80])])
        self.assertEqual(stats["migrated_blocks"], 1)
        self.assertEqual(sum(updated["histograms"]["g0_move"]["present"]), 3)
        again, _ = clean_history(updated, [], 130, 1000)
        self.assertEqual(again, updated)
        self.assertEqual(first["version"], 1, "input is unchanged")

    def test_cleanup_removes_expired_corrupt_and_duplicate_data(self):
        from tuner_under_test.history.cleanup import clean_history

        values = [5] * 18
        old = self.block(50, [(0, values)])
        current = self.block(100, [(0, values), (6, values)])
        duplicate = self.block(100, [(0, [9] * 18)])
        bad = dict(current, data="not valid base64")
        repaired = self.block(112, [(0, [200] + values[1:] + [1, 255])], 2)
        device = {
            "history": [current, old, bad, duplicate, repaired],
            "history_labels": [
                {"start": 40, "end": 110, "state": "present"},
                {"start": 100, "end": 120, "state": "not_present"},
            ],
        }
        updated, stats = clean_history(device, [], 150, 60)
        self.assertEqual(sum(b["count"] for b in updated["history"]), 3)
        self.assertEqual(stats["invalid_blocks"], 1)
        self.assertEqual(stats["expired_samples"], 1)
        self.assertEqual(stats["duplicate_samples"], 1)
        self.assertEqual(stats["repaired_samples"], 1)
        self.assertEqual(
            updated["history_labels"],
            [
                {"start": 90, "end": 100, "state": "present"},
                {"start": 100, "end": 120, "state": "not_present"},
            ],
        )
        self.assertEqual(updated["histograms"]["g0_move"]["not_present"][9], 1)
        self.assertEqual(sum(updated["histograms"]["g0_move"]["not_present"]), 2)

    def test_unknown_label_and_pending_samples_are_preserved(self):
        from tuner_under_test.history.cleanup import clean_history

        device = {
            "history": [self.block(100, [(0, [5] * 18), (6, [5] * 18)])],
            "history_labels": [
                {"start": 90, "end": 120, "state": "present"},
                {"start": 105, "end": 108, "state": "unknown"},
            ],
        }
        pending = [(112, bytes([5] * 18 + [2, 90]))]
        updated, _ = clean_history(device, pending, 130, 1000)
        self.assertEqual(sum(b["count"] for b in updated["history"]), 2)
        self.assertEqual(sum(updated["histograms"]["g0_move"]["present"]), 2)
        self.assertEqual(training._history_label_reader(updated)(106), "unknown")


class WebsocketTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.commands = {}
        ws.vol.Required = lambda key, **kwargs: key
        ws.vol.Optional = lambda key, **kwargs: key
        ws.vol.In = lambda values: values
        ws.vol.All = lambda *values: values
        ws.vol.Coerce = lambda value: value
        ws.vol.Range = lambda **kwargs: kwargs

        def schema(value):
            def decorate(function):
                function.schema = value
                return function

            return decorate

        def admin(function):
            function.admin_only = True
            return function

        ws.websocket_api.require_admin = admin
        ws.websocket_api.websocket_command = schema
        ws.websocket_api.async_response = lambda function: function
        ws.websocket_api.async_register_command = lambda hass, function: self.commands.update(
            {function.schema["type"]: function}
        )
        self.runtime = types.SimpleNamespace(
            snapshot=lambda: {"devices": {"new": {}}},
            async_learn=AsyncMock(return_value={"status": "ok"}),
            set_training_state=unittest.mock.Mock(),
            apply=AsyncMock(return_value={"applied": {}}),
        )
        self.hass = types.SimpleNamespace(data={mod.DOMAIN: self.runtime})
        self.connection = types.SimpleNamespace(
            send_result=unittest.mock.Mock(), send_error=unittest.mock.Mock()
        )
        mod._register_websocket_commands(self.hass)

    async def call(self, name, **fields):
        await self.commands[f"{mod.DOMAIN}/{name}"](self.hass, self.connection, {"id": 7, **fields})

    async def test_commands_remain_admin_only_and_lookup_live_runtime(self):
        self.assertEqual(len(self.commands), 10)
        self.assertTrue(all(command.admin_only for command in self.commands.values()))
        self.hass.data[mod.DOMAIN] = types.SimpleNamespace(snapshot=lambda: {"reloaded": True})
        await self.call("snapshot")
        self.connection.send_result.assert_called_once_with(7, {"reloaded": True})

    async def test_learning_and_applying_require_separate_explicit_commands(self):
        await self.call("learn", device_id="a")
        self.runtime.async_learn.assert_awaited_once_with("a")
        self.runtime.apply.assert_not_awaited()
        await self.call("apply", device_id="a")
        self.runtime.apply.assert_awaited_once_with("a")

    async def test_missing_runtime_and_invalid_device_preserve_error_contract(self):
        self.runtime.async_learn.side_effect = ValueError("Unknown device")
        await self.call("learn", device_id="missing")
        self.connection.send_error.assert_called_with(7, "invalid_device", "Unknown device")
        self.hass.data.clear()
        for name in self.commands:
            await self.commands[name](self.hass, self.connection, {"id": 7})
        self.connection.send_error.assert_called_with(7, "not_loaded", "LD2410 Tuner is not loaded")

    async def test_zero_timeout_still_means_no_timeout(self):
        await self.call("set_training_state", device_id="a", state="present", timeout_seconds=0)
        self.runtime.set_training_state.assert_called_once_with("a", "present", None)
        self.connection.send_result.assert_called_once_with(7, {"ok": True})


class InferenceTests(unittest.TestCase):
    def histogram(self, value):
        hist = [0] * 101
        hist[value] = 100
        return hist

    def test_weak_stationary_signal_accumulates_over_time(self):
        infer = sys.modules["tuner_under_test.presence.inference"].estimate_presence
        manual = {"g0_still": {"present": self.histogram(14), "not_present": self.histogram(10)}}
        temporal = {}
        results = [
            infer({"g0_still": 13}, manual, {}, temporal, 100 + i * 2, ["g0_still"])
            for i in range(8)
        ]
        self.assertEqual(results[-1]["label"], "present")
        self.assertGreater(results[-1]["confidence"], 0.9)
        self.assertGreater(results[-1]["presence_probability"], results[0]["presence_probability"])

    def test_move_and_still_from_same_gate_not_double_counted(self):
        infer = sys.modules["tuner_under_test.presence.inference"].estimate_presence
        manual = {
            key: {"present": self.histogram(20), "not_present": self.histogram(5)}
            for key in ("g0_move", "g0_still")
        }
        single = infer({"g0_move": 20}, manual, {}, {}, 100, ["g0_move"])
        paired = infer({"g0_move": 20, "g0_still": 20}, manual, {}, {}, 100, list(manual))
        self.assertEqual(single["score"], paired["score"])

    def test_missing_gate_is_unknown_not_absent(self):
        infer = sys.modules["tuner_under_test.presence.inference"].estimate_presence
        result = infer(
            {"g0_move": 5}, {}, {"g0_move": self.histogram(5)}, {}, 100, ["g0_move", "g1_move"]
        )
        self.assertEqual(result["label"], "unknown")

    def test_uninformative_channels_do_not_hide_learned_absence(self):
        infer = sys.modules["tuner_under_test.presence.inference"].estimate_presence
        manual = {
            "g0_still": {"present": self.histogram(20), "not_present": self.histogram(5)},
            "g0_move": {"present": self.histogram(5), "not_present": self.histogram(5)},
            "g1_move": {"present": self.histogram(5), "not_present": self.histogram(5)},
        }
        temporal = {"probability": 0.99, "timestamp": 100}
        for i in range(8):
            result = infer(
                dict.fromkeys(manual, 5), manual, {}, temporal, 100 + i * 2, list(manual)
            )
        self.assertEqual(result["label"], "not_present")
        self.assertGreater(result["confidence"], 0.9)

    def test_bootstrap_confidence_is_capped(self):
        infer = sys.modules["tuner_under_test.presence.inference"].estimate_presence
        temporal = {}
        result = None
        for i in range(10):
            result = infer(
                {"g0_move": 30},
                {},
                {"g0_move": self.histogram(5)},
                temporal,
                100 + i * 2,
                ["g0_move"],
            )
        self.assertEqual(result["label"], "present")
        self.assertEqual(result["confidence"], 0.65)

    def test_common_empty_energy_cannot_become_positive_from_label_frequency(self):
        infer = sys.modules["tuner_under_test.presence.inference"].estimate_presence
        empty = self.histogram(5)
        empty[5], empty[3] = 20, 80
        manual = {"g0_move": {"present": self.histogram(5), "not_present": empty}}
        temporal = {}
        results = [
            infer({"g0_move": 5}, manual, {}, temporal, 100 + i * 2, ["g0_move"]) for i in range(60)
        ]
        self.assertNotIn("present", [result["label"] for result in results])

    def test_novel_strong_presence_does_not_need_identical_human_example(self):
        infer = sys.modules["tuner_under_test.presence.inference"].estimate_presence
        manual = {"g0_move": {"present": self.histogram(20), "not_present": self.histogram(5)}}
        result = infer({"g0_move": 60}, manual, {}, {}, 100, ["g0_move"])
        self.assertEqual(result["label"], "present")

    def test_partial_presence_needs_direct_human_distribution_support(self):
        infer = sys.modules["tuner_under_test.presence.inference"].estimate_presence
        expected = ["g0_move", "g1_move"]
        background = {"g0_move": self.histogram(5)}
        guessed = infer({"g0_move": 30}, {}, background, {}, 100, expected)
        self.assertEqual(guessed["label"], "unknown")
        manual = {"g0_move": {"present": self.histogram(30), "not_present": self.histogram(5)}}
        guided = infer({"g0_move": 30}, manual, background, {}, 100, expected)
        self.assertEqual(guided["label"], "present")
        self.assertLessEqual(guided["confidence"], 0.60)

    def test_flat_startup_never_certifies_empty(self):
        infer = sys.modules["tuner_under_test.presence.inference"].estimate_presence
        temporal = {}
        for i in range(100):
            result = infer(
                {"g0_still": 30},
                {},
                {"g0_still": self.histogram(30)},
                temporal,
                100 + i * 2,
                ["g0_still"],
            )
            self.assertEqual(result["label"], "unknown")
            self.assertEqual(result["confidence"], 0)

    def test_weak_positive_is_reduced_by_contradictory_evidence(self):
        combine = sys.modules["tuner_under_test.presence.inference"]._combine_evidence
        self.assertLess(combine([0.1, -1, -1]), 0)
        self.assertGreater(combine([3, -1, -1]), 0)

    def test_filter_integrates_elapsed_time_without_extra_callback_votes(self):
        advance = sys.modules["tuner_under_test.presence.inference"]._advance
        regular = 0.5
        for _ in range(3):
            regular = advance(regular, 0.6, 2)
        delayed = advance(0.5, 0.6, 6)
        self.assertAlmostEqual(regular, delayed, delta=0.02)
        self.assertEqual(advance(0.5, 0.6, 0), 0.5)

    def test_confirmation_uses_time_and_resets_after_gaps(self):
        confirm = sys.modules["tuner_under_test.presence.inference"].confirm_estimate
        state = {}
        result = {"label": "present", "score": 2, "model": "test"}
        for i in range(20):
            self.assertEqual(confirm(result, state, 100 + i * 0.1), "unknown")
        self.assertEqual(confirm(result, state, 104), "present")
        self.assertEqual(confirm(result, state, 120), "unknown")
        self.assertEqual(confirm(result, state, 124), "present")
        self.assertEqual(confirm(result, state, 123), "unknown")

    def test_model_upgrade_discards_old_filter_certainty(self):
        infer = sys.modules["tuner_under_test.presence.inference"].estimate_presence
        temporal = {"probability": 0.99, "timestamp": 100, "model": "old"}
        result = infer({"g0_move": 5}, {}, {}, temporal, 102, ["g0_move"])
        self.assertEqual(result["label"], "unknown")
        self.assertEqual(result["presence_probability"], 0.5)

    def test_inference_never_mutates_human_reference_histograms(self):
        import copy

        infer = sys.modules["tuner_under_test.presence.inference"].estimate_presence
        manual = {"g0_move": {"present": self.histogram(20), "not_present": self.histogram(5)}}
        original = copy.deepcopy(manual)
        infer({"g0_move": 20}, manual, {}, {}, 100, ["g0_move"])
        self.assertEqual(manual, original)


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.states = {}
        self.registry = types.SimpleNamespace(entities={})
        mod.er.async_get = lambda hass: self.registry
        self.hass = types.SimpleNamespace(
            async_add_executor_job=lambda fn, *args: asyncio.to_thread(fn, *args),
            states=types.SimpleNamespace(get=self.states.get),
            services=types.SimpleNamespace(async_call=AsyncMock()),
            async_create_task=asyncio.create_task,
        )
        self.device = {
            "entities": {"sensor.radar_g0_move_energy": {"gate": 0, "kind": "move"}},
            "training_state": "unknown",
        }
        self.runtime = mod.TunerRuntime(
            self.hass,
            types.SimpleNamespace(async_save=AsyncMock()),
            {"devices": {"a": self.device}},
        )
        self.runtime._schedule_save = lambda: None
        self.now = time.time()

    async def asyncTearDown(self):
        tasks = list(self.runtime._timeout_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def sample(self, timestamp, value=10):
        self.states["sensor.radar_g0_move_energy"] = types.SimpleNamespace(state=str(value))
        with patch.object(time, "time", return_value=timestamp):
            self.runtime.sample_devices()

    def test_windows_share_end_and_preserve_sample_extrema(self):
        for i in range(360):
            self.runtime._record_history_sample(
                "a", {"g0_move": 95 if i == 190 else i % 20}, self.now - 21600 + i * 60
            )
        self.runtime._flush_history_block("a")
        six = self.runtime.history_series_multi("a", ["g0_move"], 6, 400, self.now)
        day = self.runtime.history_series_multi("a", ["g0_move"], 24, 400, self.now)
        self.assertEqual(six["end"], day["end"])
        for result in (six, day):
            series = result["series"]["g0_move"]
            self.assertEqual(series["sample_count"], 360)
            self.assertEqual(max(p["max"] for p in series["points"]), 95)
            self.assertEqual(min(p["min"] for p in series["points"]), 0)
        again = self.runtime.history_series_multi("a", ["g0_move"], 6, 400, self.now)
        self.assertEqual(six, again)

    async def test_same_chart_request_is_shared_and_label_changes_invalidate_cache(self):
        self.sample(self.now - 6)
        calls = []

        async def execute(fn, *args):
            calls.append(fn)
            await asyncio.sleep(0.01)
            return fn(*args)

        self.hass.async_add_executor_job = execute
        args = ("a", ["g0_move"], 6, 400, self.now)
        first, second = await asyncio.gather(
            self.runtime.async_history_series(*args), self.runtime.async_history_series(*args)
        )
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 1)
        await self.runtime.async_history_series(*args)
        self.assertEqual(len(calls), 1)
        self.runtime.label_history_range("a", self.now - 10, self.now, "present")
        updated = await self.runtime.async_history_series(*args)
        self.assertEqual(len(calls), 2)
        self.assertEqual(updated["labels"][0]["state"], "present")

    def test_fast_label_reader_matches_priority_and_boundaries(self):
        self.device["history_labels"] = [
            {"start": 0, "end": 30, "state": "present"},
            {"start": 10, "end": 20, "state": "unknown"},
            {"start": 15, "end": 25, "state": "not_present"},
        ]
        self.device.update(
            training_state="present", training_label_start=28, training_expires_at=35
        )
        read = training._history_label_reader(self.device)
        for timestamp in (-1, 0, 9, 10, 15, 19, 20, 24, 25, 28, 30, 34, 35, 100):
            self.assertEqual(
                read(timestamp), self.runtime._manual_history_state(self.device, timestamp)
            )

    async def test_changed_labels_invalidate_old_recommendation(self):
        self.configuration()
        self.device["last_learning"]["label_revision"] = 0
        self.device["label_revision"] = 1
        with self.assertRaisesRegex(ValueError, "labels changed"):
            await self.runtime.apply("a")

    def test_constant_signal_is_sampled_without_state_events(self):
        self.device.update(training_state="present", training_label_start=self.now - 1)
        for i in range(3):
            self.sample(self.now + i * 6)
        self.assertEqual(sum(self.device["histograms"]["g0_move"]["present"]), 3)

    def test_unavailable_reading_is_not_reused(self):
        self.sample(self.now)
        self.sample(self.now + 6, "unavailable")
        self.assertEqual(self.runtime._live["a"], {})
        self.assertEqual(len(list(self.runtime._iter_history_samples(self.device))), 1)

    def test_buffered_history_visible_immediately(self):
        self.sample(self.now - 2)
        result = self.runtime.history_series_multi("a", ["g0_move"], 1)
        self.assertEqual(result["series"]["g0_move"]["sample_count"], 1)
        self.assertEqual(self.device.get("history", []), [])

    def test_relabel_is_idempotent_and_does_not_duplicate_initial_training(self):
        self.device.update(training_state="present", training_label_start=self.now - 20)
        self.sample(self.now - 12)
        self.sample(self.now - 6)
        for _ in range(2):
            self.runtime.label_history_range("a", self.now - 15, self.now - 1, "not_present")
        hist = self.device["histograms"]["g0_move"]
        self.assertEqual(sum(hist["present"]), 0)
        self.assertEqual(sum(hist["not_present"]), 2)

    def test_unknown_clears_only_requested_half_open_range(self):
        self.device.update(training_state="present", training_label_start=self.now - 20)
        self.sample(self.now - 12)
        self.sample(self.now - 6)
        self.runtime.label_history_range("a", self.now - 12, self.now - 6, "unknown")
        hist = self.device["histograms"]["g0_move"]
        self.assertEqual(sum(hist["present"]), 1)

    def test_empty_auto_training_is_unknown(self):
        result = self.runtime._classify_auto(self.device, {"g0_move": 50})
        self.assertEqual(result["label"], "unknown")

    def test_clear_invalidates_learned_and_stops_training(self):
        self.device.update(
            training_state="present",
            training_label_start=self.now - 20,
            last_learning={"status": "ok"},
        )
        self.sample(self.now - 6)
        self.runtime.clear_samples("a")
        self.assertNotIn("last_learning", self.device)
        self.assertEqual(self.device["training_state"], "unknown")
        self.assertEqual(list(self.runtime._iter_history_samples(self.device)), [])

    def test_unknown_device_training_rejected(self):
        with self.assertRaises(ValueError):
            self.runtime.set_training_state("bogus", "present")
        self.assertNotIn("bogus", self.runtime.data["devices"])

    def test_long_history_gap_does_not_overflow_offset(self):
        self.runtime._record_history_sample("a", {"g0_move": 10}, self.now - 70000)
        self.runtime._record_history_sample("a", {"g0_move": 20}, self.now)
        timestamps = [ts for ts, _ in self.runtime._iter_history_samples(self.device)]
        self.assertEqual(timestamps, [self.now - 70000, self.now])

    async def test_cancelled_timeout_cannot_remove_replacement(self):
        self.runtime.set_training_state("a", "present", 100)
        await asyncio.sleep(0)
        old_task = self.runtime._timeout_tasks["a"]
        self.runtime.set_training_state("a", "not_present", 200)
        new_task = self.runtime._timeout_tasks["a"]
        await asyncio.sleep(0)
        self.assertIs(self.runtime._timeout_tasks["a"], new_task)
        self.assertTrue(old_task.cancelled())

    def test_expired_training_does_not_label_restart_gap(self):
        self.device.update(
            training_state="present",
            training_label_start=self.now - 30,
            training_expires_at=self.now - 20,
        )
        self.runtime._close_training_interval(self.device, self.now)
        self.assertEqual(self.device["history_labels"][0]["end"], self.now - 20)

    def configuration(self):
        for i in range(3):
            entity_id = f"number.radar_g{i}_move_threshold"
            self.registry.entities[entity_id] = types.SimpleNamespace(
                device_id="a", domain="number", entity_id=entity_id
            )
            self.states[entity_id] = types.SimpleNamespace(state="10")
        for i in range(3):
            still_id = f"number.radar_g{i}_still_threshold"
            self.registry.entities[still_id] = types.SimpleNamespace(
                device_id="a", domain="number", entity_id=still_id
            )
            self.states[still_id] = types.SimpleNamespace(state="10")
        for kind, limit in (("move", 2), ("still", 2)):
            entity_id = f"number.radar_max_{kind}_distance_gate"
            self.registry.entities[entity_id] = types.SimpleNamespace(
                device_id="a", domain="number", entity_id=entity_id
            )
            self.states[entity_id] = types.SimpleNamespace(state=str(limit))
        entities, current = self.runtime._threshold_configuration("a")
        self.device["last_learning"] = {
            "method": "human_priority_v4",
            "status": "ok",
            "entities": entities,
            "configuration": current,
            "proposals": {key: {"threshold": 20} for key in entities},
        }

    async def test_apply_stops_after_partial_failure(self):
        self.configuration()
        self.hass.services.async_call.side_effect = [None, RuntimeError("offline")]
        result = await self.runtime.apply("a")
        self.assertEqual(len(result["applied"]), 1)
        self.assertEqual(len(result["skipped"]), 5)
        self.assertEqual(self.hass.services.async_call.await_count, 2)

    async def test_apply_requires_preview_and_unchanged_configuration(self):
        with self.assertRaises(ValueError):
            await self.runtime.apply("a")
        self.configuration()
        self.states["number.radar_g0_move_threshold"].state = "30"
        with self.assertRaises(ValueError):
            await self.runtime.apply("a")
        self.hass.services.async_call.assert_not_awaited()

    def test_nonfinite_history_range_rejected(self):
        with self.assertRaises(ValueError):
            self.runtime.label_history_range("a", float("nan"), self.now, "present")
        with self.assertRaises(ValueError):
            self.runtime.history_series_multi("a", ["g0_move"], float("inf"))

    def test_unmanaged_gate_blocks_device_wide_learning(self):
        self.configuration()
        self.registry.entities.pop("number.radar_g1_move_threshold")
        with self.assertRaisesRegex(ValueError, "Enable all active"):
            self.runtime._threshold_configuration("a")

    async def test_chart_executor_includes_pending_data(self):
        self.sample(self.now - 2)
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
            values = dict.fromkeys(keys, 5)
            if i < 100:
                values["g0_move" if i % 2 else "g2_still"] = 30
            row = bytes(values.get(key, 255) for key in constants.HISTORY_KEYS)
            samples.append((start + i * 6, row))
        self.runtime._history_runtime["a"] = {"samples": samples}
        self.device["history_labels"] = [
            {"start": start, "end": start + 600, "state": "present"},
            {"start": start + 600, "end": self.now, "state": "not_present"},
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
        for i in range(45):
            self.sample(self.now + i * 2, 5)
        self.assertEqual(self.device["auto"]["last_classification"]["state"], "unknown")
        for i in range(45, 55):
            self.sample(self.now + i * 2, 30)
        last = self.device["auto"]["last_classification"]
        self.assertEqual(last["state"], "present")
        self.assertEqual(last["confidence"], 0.65)
        self.assertEqual(last["basis"], "bootstrap")

    def test_short_spike_cannot_confirm_entry_from_filter_memory(self):
        for i in range(45):
            self.sample(self.now + i * 2, 5)
        states = []
        for i in range(45, 65):
            self.sample(self.now + i * 2, 30 if i < 47 else 5)
            states.append(self.device["auto"]["last_classification"]["state"])
        self.assertNotIn("present", states)

    def test_confidence_survives_history_flush_and_old_blocks_still_read(self):
        self.device["auto"] = {
            "last_classification": {"state": "present", "confidence": 0.73, "timestamp": self.now}
        }
        self.runtime._record_history_sample("a", {"g0_move": 20}, self.now)
        self.runtime._flush_history_block("a")
        self.assertEqual(self.device["history"][0]["version"], 2)
        row = list(self.runtime._iter_history_samples(self.device, include_auto=True))[0][1]
        self.assertEqual(tuple(row[-2:]), (1, 73))
        self.assertEqual(len(list(self.runtime._iter_history_samples(self.device))[0][1]), 18)
        old_raw = struct.pack(">H", 0) + bytes([5] * 18)
        self.device["history"].append(
            {
                "start": self.now + 6,
                "count": 1,
                "data": base64.b64encode(zlib.compress(old_raw)).decode(),
            }
        )
        rows = list(self.runtime._iter_history_samples(self.device, include_auto=True))
        self.assertEqual(len(rows[-1][1]), 18)

    def test_manual_corrections_and_explicit_unknown_override_guesses(self):
        self.configuration()
        keys = list(self.device["last_learning"]["entities"])
        values = bytes(30 if key in keys else 255 for key in constants.HISTORY_KEYS)
        self.runtime._history_runtime["a"] = {
            "samples": [
                (self.now - 12, values + bytes((1, 90))),
                (self.now - 6, values + bytes((1, 90))),
            ]
        }
        self.runtime.label_history_range("a", self.now - 15, self.now - 9, "not_present")
        self.runtime.label_history_range("a", self.now - 9, self.now - 1, "unknown")
        entities, current = self.runtime._threshold_configuration("a")
        result = self.runtime._fit_history("a", entities, current)
        self.assertEqual(result["counts"]["not_present"], 1)
        self.assertEqual(result["automatic_evidence"]["samples"]["present"], 0)

    async def test_persisted_guesses_reach_learner_with_confidence(self):
        self.configuration()
        keys = list(self.device["last_learning"]["entities"])
        samples = []
        for i in range(200):
            values = dict.fromkeys(keys, 5)
            if i < 100:
                values["g0_move"] = 30
            row = bytes(values.get(key, 255) for key in constants.HISTORY_KEYS)
            samples.append((self.now - 1200 + i * 6, row + bytes((1 if i < 100 else 2, 65))))
        self.runtime._history_runtime["a"] = {"samples": samples}
        self.runtime._flush_history_block("a")
        result = await self.runtime.async_learn("a")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["evidence_basis"], "automatic")
        self.assertEqual(
            result["automatic_evidence"]["samples"], {"present": 100, "not_present": 100}
        )
        self.assertAlmostEqual(result["automatic_evidence"]["mean_confidence"]["present"], 0.65)
        applied = await self.runtime.apply("a")
        self.assertEqual(set(applied["applied"]), set(keys))

    async def test_previous_95_percent_model_cannot_be_applied(self):
        self.configuration()
        for previous in ("joint_labelled_v1", "human_priority_v3"):
            self.device["last_learning"]["method"] = previous
            with self.assertRaises(ValueError):
                await self.runtime.apply("a")
        self.hass.services.async_call.assert_not_awaited()

    async def test_format_cleanup_preserves_current_model_recommendation(self):
        self.sample(self.now - 10)
        self.runtime._flush_history_block("a")
        self.device["last_learning"] = {
            "method": sys.modules["tuner_under_test.calibration.fitting"].METHOD,
            "status": "ok",
        }
        saved = self.device["last_learning"]
        self.hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *args: fn(*args))
        await self.runtime.async_clean_history(persist=False)
        self.assertIs(self.device["last_learning"], saved)
        self.assertIn("history_cleanup", self.device)

    async def test_invalid_history_cleanup_invalidates_recommendation(self):
        self.device["history"] = [{"data": "corrupt"}]
        self.device["last_learning"] = {
            "method": sys.modules["tuner_under_test.calibration.fitting"].METHOD,
            "status": "ok",
        }
        self.hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *args: fn(*args))
        await self.runtime.async_clean_history(persist=False)
        self.assertNotIn("last_learning", self.device)
        self.assertEqual(self.device["history"], [])
        self.assertEqual(self.device["history_cleanup"]["invalid_blocks"], 1)

    async def test_cleanup_does_not_overwrite_concurrent_sample(self):
        self.sample(self.now - 10)
        self.runtime._flush_history_block("a")
        original = self.runtime.data["devices"]["a"]["history"]

        def executor(fn, *args):
            result = fn(*args)
            self.sample(self.now)
            return result

        self.hass.async_add_executor_job = AsyncMock(side_effect=executor)
        await self.runtime.async_clean_history()
        self.assertIs(self.device["history"], original)
        self.assertEqual(len(self.runtime._history_runtime["a"]["samples"]), 1)
        self.assertNotIn("history_cleanup", self.device)

    async def test_unload_flushes_partial_history(self):
        self.sample(self.now - 2)
        self.hass.data = {mod.DOMAIN: self.runtime}
        mod.frontend.async_panel_exists = lambda *a: False
        await mod.async_unload_entry(self.hass, None)
        self.assertEqual(self.device["history"][0]["count"], 1)
        self.runtime.store.async_save.assert_awaited_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
