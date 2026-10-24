"""Seeded production lifecycle operations preserve the full retained-history answers."""

import random
import unittest
from copy import deepcopy
from unittest.mock import patch

import test_tuner as harness
from test_efficiency import DAY, NOW
from tuner_under_test.history import labels
from tuner_under_test.presence import reference_training as training
from tuner_under_test.rooms import attribution, groups, room_labels


class MaintainedLifecycleTests(unittest.IsolatedAsyncioTestCase):
    setUp = harness.RuntimeTests.setUp
    asyncTearDown = harness.RuntimeTests.asyncTearDown

    def fresh_runtime(self):
        """A restarted runtime: the same recorded history (the day store) and data, no caches."""
        fresh = harness.mod.TunerRuntime(self.hass, None, deepcopy(self.runtime.data))
        fresh._days = self.runtime._days
        fresh._schedule_save = lambda: None  # Persistence is the external HA boundary.
        return fresh

    def assert_full_oracles(self, event):
        """Cached answers (prepared job views, kept attribution, maintained references) equal
        a fresh runtime's recomputation after every lifecycle step."""
        runtime = self.runtime
        oracle = self.fresh_runtime()
        for device_id in runtime.data["devices"]:
            with self.subTest(event=event, device=device_id):
                saved = attribution.snapshot(oracle, device_id)
                view = attribution.prepared_view(runtime, device_id)
                if saved is not None:
                    for human in (True, False):
                        full = attribution.radar_attribution(oracle, device_id, saved, human)
                        fast = attribution.radar_attribution(
                            view, device_id, view.data["room_snapshot"], human
                        )
                        self.assertEqual(list(fast["times"]), list(full["times"]))
                        self.assertEqual(fast["found"], full["found"])
                        if event == "append-3":
                            self.assertTrue(
                                any(
                                    result["state"] == "yes"
                                    for found in full["found"].values()
                                    for _, result in found
                                ),
                                "Fixture must exercise an occupied attribution, not only unknowns",
                            )
                device = runtime.data["devices"][device_id]
                training.rebuild_human(runtime, device)
                fresh = oracle.data["devices"][device_id]
                training.rebuild_human(oracle, fresh)
                self.assertEqual(device["reference_profile"], fresh["reference_profile"])

    async def test_seeded_append_flush_edit_membership_prune_clear_and_restart(self):
        rng = random.Random(841)
        runtime = self.runtime
        runtime.data["devices"]["b"] = deepcopy(self.device)
        start = NOW - 2 * DAY
        timestamp = start
        with patch("time.time", return_value=NOW):
            groups.configure(runtime, None, "Seeded room", None, ["a", "b"])
            group_id = next(iter(runtime.data["room_groups"]))
            room_labels.label_room_range(runtime, group_id, start, start + 900, "not_present")
            room_labels.label_room_range(runtime, group_id, start + 900, NOW, "present")
            self.assert_full_oracles("empty")
            for batch in range(4):
                for _ in range(70):
                    timestamp += rng.choice((6, 6, 6, 7, 40))
                    for device_id in ("a", "b"):
                        value = (
                            rng.choice((0, 5, 15))
                            if timestamp < start + 900
                            else rng.choice((45, 80, 100))
                        )
                        runtime._record_history_sample(device_id, {"g0_move": value}, timestamp)
                self.assert_full_oracles(f"append-{batch}")
                if batch % 2 == 0:
                    runtime._days.flush(NOW)
                    self.assert_full_oracles(f"flush-{batch}")

            await labels.label_history_range(runtime, "a", start + 400, start + 700, "not_present")
            self.assert_full_oracles("own-label-add")
            labels.edit_history_label(
                runtime,
                "a",
                start + 400,
                start + 700,
                start + 500,
                start + 800,
                "unknown",
                self.device["label_revision"],
            )
            self.assert_full_oracles("own-label-move-exclusion")
            labels.edit_history_label(
                runtime,
                "a",
                start + 500,
                start + 800,
                start + 500,
                start + 800,
                "unlabelled",
                self.device["label_revision"],
            )
            self.assert_full_oracles("own-label-delete")
            room_labels.label_room_range(
                runtime, group_id, start + 400, start + 1800, "not_present"
            )
            self.assert_full_oracles("quiet-baseline-change")
            groups.configure(runtime, group_id, "Seeded room", None, ["a"])
            self.assert_full_oracles("member-remove")
            groups.configure(runtime, group_id, "Seeded room", None, ["a", "b"])
            self.assert_full_oracles("member-restore")
            runtime.data["storage_settings"] = {"human_days": 1}
            for device_id in ("a", "b"):
                runtime._record_history_sample(device_id, {"g0_move": 5}, NOW - 12)
            runtime._days.flush(NOW)
            self.assert_full_oracles("retention-prune")
            self.runtime = self.fresh_runtime()
            runtime = self.runtime
            self.assert_full_oracles("restart")
            runtime.clear_samples("a")
            self.assert_full_oracles("clear")
            runtime._record_history_sample("a", {"g0_move": 100}, NOW - 6)
            self.assert_full_oracles("append-after-clear")
