"""Room labels share the device label semantics but live on groups."""

import types
import unittest
from copy import deepcopy

import test_rooms as rooms_harness
import test_tuner as harness
from tuner_under_test.rooms import groups, jobs, room_labels

STALE = "History labels changed. Refresh and select the period again."


class RoomLabelTests(unittest.IsolatedAsyncioTestCase):
    setUp = harness.RuntimeTests.setUp
    asyncTearDown = harness.RuntimeTests.asyncTearDown
    office = rooms_harness.GroupTests.office

    def zone(self):
        self.office()
        return groups.configure(self.runtime, None, "Desk", "office", ["a"])["id"]

    def test_label_room_range_stores_and_bumps_revision(self):
        self.office()
        result = room_labels.label_room_range(self.runtime, "area:office", 10, 20, "present")
        self.assertTrue(result["ok"])
        self.assertEqual(result["revision"], 1)
        expected = [{"start": 10, "end": 20, "state": "present", "source": "manual"}]
        self.assertEqual(result["room_labels"], expected)
        self.assertEqual(room_labels.room_labels_for(self.runtime, "area:office"), expected)
        self.assertEqual(self.runtime.data["room_label_revision"]["area:office"], 1)

    def test_overlapping_room_labels_preserve_outer_parts(self):
        self.office()
        room_labels.label_room_range(self.runtime, "area:office", 10, 40, "present")
        result = room_labels.label_room_range(self.runtime, "area:office", 20, 30, "not_present")
        spans = sorted((x["start"], x["end"], x["state"]) for x in result["room_labels"])
        self.assertEqual(spans, [(10, 20, "present"), (20, 30, "not_present"), (30, 40, "present")])

    def test_edit_moves_period_and_unlabelled_removes_it(self):
        self.office()
        room_labels.label_room_range(self.runtime, "area:office", 10, 20, "present")
        result = room_labels.edit_room_label(
            self.runtime, "area:office", 10, 20, 12, 18, "not_present", 1
        )
        self.assertEqual(
            [(x["start"], x["end"], x["state"]) for x in result["room_labels"]],
            [(12, 18, "not_present")],
        )
        result = room_labels.edit_room_label(
            self.runtime, "area:office", 12, 18, 12, 18, "unlabelled", 2
        )
        self.assertEqual(result["room_labels"], [])
        self.assertEqual(result["revision"], 3)

    def test_stale_room_edit_rejected(self):
        self.office()
        room_labels.label_room_range(self.runtime, "area:office", 10, 20, "present")
        before = deepcopy(self.runtime.data)
        with self.assertRaisesRegex(ValueError, STALE):
            room_labels.edit_room_label(self.runtime, "area:office", 10, 20, 10, 20, "unknown", 0)
        self.assertEqual(self.runtime.data, before)

    def test_missing_period_and_invalid_input_rejected(self):
        self.office()
        room_labels.label_room_range(self.runtime, "area:office", 10, 20, "present")
        with self.assertRaisesRegex(ValueError, "no longer exists"):
            room_labels.edit_room_label(self.runtime, "area:office", 11, 20, 10, 20, "unknown", 1)
        for args in ((20, 10, "present"), (10, float("nan"), "present"), (10, 20, "bogus")):
            with self.subTest(args=args), self.assertRaises(ValueError):
                room_labels.label_room_range(self.runtime, "area:office", *args)
        self.assertEqual(self.runtime.data["room_label_revision"]["area:office"], 1)

    def test_unknown_group_rejected(self):
        self.office()
        with self.assertRaisesRegex(ValueError, "no longer exists"):
            room_labels.label_room_range(self.runtime, "zone:missing", 10, 20, "present")
        with self.assertRaisesRegex(ValueError, "no longer exists"):
            room_labels.edit_room_label(self.runtime, "zone:missing", 10, 20, 10, 20, "present", 0)
        self.assertNotIn("room_labels", self.runtime.data)

    def test_write_marks_saved_assessment_stale(self):
        self.office()
        _, signature = jobs._context(self.runtime, "area:office")
        self.runtime.data.setdefault("room_assessments", {})["area:office"] = {
            "signature": signature,
            "report": {"state": "ready"},
        }
        self.assertEqual(jobs.summary(self.runtime, "area:office")["state"], "ready")
        room_labels.label_room_range(self.runtime, "area:office", 10, 20, "present")
        self.assertEqual(jobs.summary(self.runtime, "area:office")["state"], "stale")

    def test_restore_area_default_keeps_area_labels(self):
        self.office()
        groups.configure(self.runtime, "area:office", "Office", "office", ["b"])
        room_labels.label_room_range(self.runtime, "area:office", 10, 20, "present")
        groups.remove(self.runtime, "area:office")
        self.assertEqual(
            groups.definitions(self.runtime)[2]["area:office"]["device_ids"], ["a", "b"]
        )
        self.assertEqual(len(room_labels.room_labels_for(self.runtime, "area:office")), 1)
        self.assertEqual(self.runtime.data["room_label_revision"]["area:office"], 1)

    def test_remove_zone_drops_its_labels(self):
        zone = self.zone()
        kept = groups.configure(self.runtime, None, "Sofa", "office", ["b"])["id"]
        room_labels.label_room_range(self.runtime, zone, 10, 20, "present")
        room_labels.label_room_range(self.runtime, kept, 10, 20, "present")
        groups.remove(self.runtime, zone)
        self.assertNotIn(zone, self.runtime.data["room_labels"])
        self.assertNotIn(zone, self.runtime.data["room_label_revision"])
        self.assertEqual(len(room_labels.room_labels_for(self.runtime, kept)), 1)

    def test_removing_group_works_when_label_keys_are_absent(self):
        zone = self.zone()
        self.assertNotIn("room_labels", self.runtime.data)
        groups.remove(self.runtime, zone)
        self.assertEqual(room_labels.room_labels_for(self.runtime, zone), [])

    def test_snapshot_exposes_labels_and_revision(self):
        self.office()
        for key in ("a", "b"):
            self.registry.entities[f"sensor.{key}_g0_move_energy"] = types.SimpleNamespace(
                entity_id=f"sensor.{key}_g0_move_energy", device_id=key, domain="sensor"
            )
        room_labels.label_room_range(self.runtime, "area:office", 10, 20, "present")
        group = self.runtime.snapshot()["rooms"]["groups"]["area:office"]
        self.assertEqual(group["label_revision"], 1)
        self.assertEqual(group["labels"][0]["state"], "present")
        zone = groups.configure(self.runtime, None, "Desk", "office", ["a"])["id"]
        bare = self.runtime.snapshot()["rooms"]["groups"][zone]
        self.assertEqual((bare["labels"], bare["label_revision"]), ([], 0))

    async def test_websocket_dispatch_through_coordinator(self):
        self.office()
        result = await harness.ws._websocket_result(
            self.runtime,
            "label_room_range",
            ("group_id", "start", "end", "state"),
            {"group_id": "area:office", "start": 10, "end": 20, "state": "present"},
        )
        self.assertEqual(result["revision"], 1)
        result = await harness.ws._websocket_result(
            self.runtime,
            "edit_room_label",
            ("group_id", "label_start", "label_end", "start", "end", "state", "revision"),
            {
                "group_id": "area:office",
                "label_start": 10,
                "label_end": 20,
                "start": 10,
                "end": 20,
                "state": "not_present",
                "revision": 1,
            },
        )
        self.assertEqual(result["room_labels"][0]["state"], "not_present")

    def seed_sensor(self, *spans):
        self.runtime.data.setdefault("room_labels", {})["area:office"] = [
            {"start": a, "end": b, "state": state, "source": "sensor"} for a, b, state in spans
        ]

    def sources(self, labels):
        return sorted((x["start"], x["end"], x["state"], x["source"]) for x in labels)

    def test_adjacent_same_state_manual_and_sensor_labels_stay_separate(self):
        self.office()
        self.seed_sensor((10, 20, "present"))
        result = room_labels.label_room_range(self.runtime, "area:office", 20, 30, "present")
        self.assertEqual(
            self.sources(result["room_labels"]),
            [(10, 20, "present", "sensor"), (20, 30, "present", "manual")],
        )

    def test_manual_write_overlapping_sensor_label_leaves_it_intact(self):
        self.office()
        self.seed_sensor((10, 40, "not_present"))
        result = room_labels.label_room_range(self.runtime, "area:office", 20, 30, "present")
        self.assertEqual(
            self.sources(result["room_labels"]),
            [(10, 40, "not_present", "sensor"), (20, 30, "present", "manual")],
        )

    def test_editing_manual_label_never_alters_sensor_labels(self):
        self.office()
        self.seed_sensor((10, 20, "present"))
        room_labels.label_room_range(self.runtime, "area:office", 10, 20, "present")
        result = room_labels.edit_room_label(
            self.runtime, "area:office", 10, 20, 12, 18, "not_present", 1
        )
        self.assertEqual(
            self.sources(result["room_labels"]),
            [(10, 20, "present", "sensor"), (12, 18, "not_present", "manual")],
        )
        result = room_labels.edit_room_label(
            self.runtime, "area:office", 12, 18, 12, 18, "unlabelled", 2
        )
        self.assertEqual(self.sources(result["room_labels"]), [(10, 20, "present", "sensor")])
        with self.assertRaisesRegex(ValueError, "no longer exists"):
            room_labels.edit_room_label(
                self.runtime, "area:office", 10, 20, 10, 20, "unlabelled", 3
            )
