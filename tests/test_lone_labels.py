"""A room or zone with exactly one radar is that radar: its manual labels train it."""

import unittest

import test_rooms as rooms_harness
import test_tuner as harness
from tuner_under_test.history.label_lookup import _history_label_reader
from tuner_under_test.rooms import groups, lone_labels, membership, room_labels


class LoneLabelTests(unittest.IsolatedAsyncioTestCase):
    setUp = harness.RuntimeTests.setUp
    asyncTearDown = harness.RuntimeTests.asyncTearDown
    office = rooms_harness.GroupTests.office

    def lone_zone(self):
        self.office()
        return groups.configure(self.runtime, None, "Desk", "office", ["a"])["id"]

    def radar(self, key="a"):
        return self.runtime.data["devices"][key]

    def test_lone_radar_reads_group_manual_label(self):
        zone = self.lone_zone()
        room_labels.label_room_range(self.runtime, zone, 100, 200, "present")
        read = _history_label_reader(self.radar())
        self.assertEqual(read(150), "present")
        self.assertIsNone(read(250))
        self.assertNotIn("group_labels", self.radar("b"))

    def test_sync_reports_the_radars_it_changed(self):
        zone = self.lone_zone()
        self.runtime.data.setdefault("room_labels", {})[zone] = [
            {"start": 100, "end": 200, "state": "present", "source": "manual"}
        ]
        self.assertEqual(lone_labels.sync(self.runtime), {"a"})
        self.assertEqual(lone_labels.sync(self.runtime), set())
        self.assertEqual(
            self.radar()["group_labels"], [{"start": 100, "end": 200, "state": "present"}]
        )

    def test_own_label_wins_over_group_label(self):
        zone = self.lone_zone()
        self.radar()["history_labels"] = [{"start": 120, "end": 140, "state": "not_present"}]
        room_labels.label_room_range(self.runtime, zone, 100, 200, "present")
        read = _history_label_reader(self.radar())
        self.assertEqual(read(130), "not_present")
        self.assertEqual(read(150), "present")

    def test_sensor_room_labels_are_not_copied(self):
        zone = self.lone_zone()
        room_labels.write_sensor_range(self.runtime, zone, 100, 200, "present", 0.9)
        lone_labels.sync(self.runtime)
        self.assertNotIn("group_labels", self.radar())

    def test_second_member_removes_group_labels_and_retrains(self):
        zone = self.lone_zone()
        room_labels.label_room_range(self.runtime, zone, 100, 200, "present")
        self.radar()["last_learning"] = {"proposals": {}}
        revision = self.radar().get("label_revision", 0)
        groups.configure(self.runtime, zone, "Desk", "office", ["a", "b"])
        self.assertNotIn("group_labels", self.radar())
        self.assertGreater(self.radar()["label_revision"], revision)
        self.assertNotIn("last_learning", self.radar())

    def test_room_label_edit_retrains_lone_radar(self):
        zone = self.lone_zone()
        revision = self.radar().get("label_revision", 0)
        room_labels.label_room_range(self.runtime, zone, 100, 200, "present")
        self.assertGreater(self.radar()["label_revision"], revision)

    def test_paused_radar_leaves_its_area_and_its_labels(self):
        self.office()
        self.radar("b")["recording_enabled"] = False
        before = groups.definitions(self.runtime)[2]
        membership.membership_changed(self.runtime, before)
        room_labels.label_room_range(self.runtime, "area:office", 100, 200, "present")
        self.assertEqual(_history_label_reader(self.radar())(150), "present")
        before = groups.definitions(self.runtime)[2]
        self.radar()["recording_enabled"] = False
        membership.membership_changed(self.runtime, before)
        self.assertNotIn("area:office", groups.definitions(self.runtime)[2])
        self.assertNotIn("group_labels", self.radar())

    def test_paused_radar_in_a_saved_zone_derives_nothing(self):
        zone = self.lone_zone()
        self.radar()["recording_enabled"] = False
        room_labels.label_room_range(self.runtime, zone, 100, 200, "present")
        self.assertNotIn("group_labels", self.radar())

    def test_zone_with_one_recording_member_is_that_radar(self):
        self.office()
        zone = groups.configure(self.runtime, None, "Desk", "office", ["a", "b"])["id"]
        self.radar("b")["recording_enabled"] = False
        room_labels.label_room_range(self.runtime, zone, 100, 200, "present")
        self.assertEqual(_history_label_reader(self.radar())(150), "present")
        self.assertNotIn("group_labels", self.radar("b"))

    def test_startup_refresh_keeps_learning_results(self):
        zone = self.lone_zone()
        self.runtime.data.setdefault("room_labels", {})[zone] = [
            {"start": 100, "end": 200, "state": "present", "source": "manual"}
        ]
        self.radar()["last_learning"] = {"proposals": {}}
        revision = self.radar().get("label_revision", 0)
        self.assertEqual(lone_labels.refresh(self.runtime), {"a"})
        self.assertIn("group_labels", self.radar())
        self.assertGreater(self.radar()["label_revision"], revision)
        self.assertIn("last_learning", self.radar())
        self.assertEqual(lone_labels.refresh(self.runtime), set())

    def test_live_samples_take_the_group_label_unless_the_radar_has_its_own(self):
        zone = self.lone_zone()
        room_labels.label_room_range(self.runtime, zone, 100, 200, "present")
        self.radar()["history_labels"] = [{"start": 120, "end": 140, "state": "not_present"}]
        state = self.runtime._manual_history_state
        self.assertEqual(state(self.radar(), 150), "present")
        self.assertEqual(state(self.radar(), 130), "not_present")
        self.assertIsNone(state(self.radar(), 250))


def test_earlier_view_clips_group_labels_at_the_cutoff():
    from types import SimpleNamespace

    from tuner_under_test.calibration.adaptive import earlier_view

    device = {
        "history_labels": [],
        "group_labels": [
            {"start": 0, "end": 10, "state": "present"},
            {"start": 20, "end": 30, "state": "not_present"},
        ],
    }
    view = SimpleNamespace(data={"devices": {"a": device}})
    earlier = earlier_view(view, 5)
    assert earlier.data["devices"]["a"]["group_labels"] == [
        {"start": 0, "end": 5, "state": "present"}
    ]
    assert len(device["group_labels"]) == 2  # The original view is untouched.
