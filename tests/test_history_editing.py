"""Saved manual period editing must preserve neighboring and automatic evidence."""

import unittest
from copy import deepcopy
from unittest.mock import patch

import test_tuner as harness


class HistoryEditingTests(unittest.IsolatedAsyncioTestCase):
    setUp = harness.RuntimeTests.setUp
    asyncTearDown = harness.RuntimeTests.asyncTearDown
    configuration = harness.RuntimeTests.configuration

    def test_move_period_removes_old_bounds_and_splits_overlapping_neighbor(self):
        self.device["history_labels"] = [
            {"start": 10, "end": 20, "state": "present"},
            {"start": 20, "end": 40, "state": "not_present"},
        ]
        self.device["last_learning"] = {"status": "ok"}
        self.runtime.edit_history_label("a", 10, 20, 15, 25, "unknown", 0)
        self.assertIsNone(self.runtime._manual_history_state(self.device, 12))
        self.assertEqual(self.runtime._manual_history_state(self.device, 17), "unknown")
        self.assertEqual(self.runtime._manual_history_state(self.device, 22), "unknown")
        self.assertEqual(self.runtime._manual_history_state(self.device, 27), "not_present")
        self.assertEqual(self.device["label_revision"], 1)
        self.assertNotIn("last_learning", self.device)

    def test_remove_unknown_restores_stored_guesses_without_deleting_recordings(self):
        self.configuration()
        keys = list(self.device["last_learning"]["entities"])
        row = bytes(30 if key in keys else 255 for key in harness.constants.HISTORY_KEYS)
        samples = [(self.now - 12, row + bytes((1, 90)))]
        self.runtime._history_runtime["a"] = {"samples": samples}
        self.runtime.label_history_range("a", self.now - 20, self.now - 1, "unknown")
        entities, current = self.runtime._threshold_configuration("a")
        before = self.runtime._fit_history("a", entities, current)
        self.assertEqual(before["automatic_evidence"]["samples"]["present"], 0)
        self.runtime.edit_history_label(
            "a", self.now - 20, self.now - 1, self.now - 20, self.now - 1, "unlabelled", 1
        )
        after = self.runtime._fit_history("a", entities, current)
        self.assertEqual(after["automatic_evidence"]["samples"]["present"], 1)
        self.assertEqual(self.device["history_labels"], [])
        self.assertEqual(self.runtime._history_runtime["a"]["samples"], samples)
        self.hass.services.async_call.assert_not_called()

    def test_edit_rebuilds_human_counts_for_the_new_boundaries(self):
        base = self.now - 60
        self.device["history_labels"] = [{"start": base + 10, "end": base + 40, "state": "present"}]
        row = bytes([30] * len(harness.constants.HISTORY_KEYS))
        self.runtime._history_runtime["a"] = {
            "samples": [(base + offset, row) for offset in (15, 25, 35)]
        }
        self.runtime.edit_history_label(
            "a", base + 10, base + 40, base + 20, base + 30, "not_present", 0
        )
        series = self.device["histograms"]["g0_move"]
        self.assertEqual(sum(series["present"]), 0)
        self.assertEqual(sum(series["not_present"]), 1)
        self.assertEqual(series["not_present"][30], 1)

    def test_stale_missing_and_invalid_edits_do_not_mutate_data(self):
        self.device["history_labels"] = [{"start": 10, "end": 20, "state": "present"}]
        self.device["label_revision"] = 2
        before = deepcopy(self.device)
        for args in [
            (10, 20, 10, 20, "unknown", 1),
            (11, 20, 10, 20, "unknown", 2),
            (10, 20, 20, 10, "unknown", 2),
            (10, 20, 10, float("nan"), "unknown", 2),
            (10, 20, 10, 20, "invalid", 2),
        ]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.runtime.edit_history_label("a", *args)
            self.assertEqual(self.device, before)
        with self.assertRaisesRegex(ValueError, "Unknown device"):
            self.runtime.edit_history_label("missing", 10, 20, 10, 20, "present", 0)

    def test_edit_closes_live_interval_but_keeps_current_training_active(self):
        self.device.update(
            history_labels=[{"start": 10, "end": 20, "state": "present"}],
            training_state="not_present",
            training_label_start=20,
        )
        with patch.object(harness.training.time, "time", return_value=40):
            self.runtime.edit_history_label("a", 10, 20, 12, 18, "unknown", 0)
        self.assertEqual(self.device["training_state"], "not_present")
        self.assertEqual(self.device["training_label_start"], 40)
        self.assertEqual(self.runtime._manual_history_state(self.device, 30), "not_present")
        self.assertIsNone(self.runtime._manual_history_state(self.device, 11))

    async def test_edit_websocket_dispatch_forwards_revision_and_boundaries(self):
        self.device["history_labels"] = [{"start": 10, "end": 20, "state": "present"}]
        result = await harness.ws._websocket_result(
            self.runtime,
            "edit_history_label",
            ("device_id", "label_start", "label_end", "start", "end", "state", "revision"),
            {
                "device_id": "a",
                "label_start": 10,
                "label_end": 20,
                "start": 10,
                "end": 20,
                "state": "not_present",
                "revision": 0,
            },
        )
        self.assertTrue(result["ok"])
        self.assertEqual(self.runtime._manual_history_state(self.device, 15), "not_present")
