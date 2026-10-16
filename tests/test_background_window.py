"""Explicit Seen-by approximation preserves the exact default and stored evidence."""

import unittest
from copy import deepcopy
from unittest.mock import patch

import test_rooms
from tuner_under_test.rooms import attribution, groups, seen_by


class BackgroundWindowTests(unittest.IsolatedAsyncioTestCase):
    setUp = test_rooms.RoomAttributionTests.setUp
    asyncTearDown = test_rooms.RoomAttributionTests.asyncTearDown
    office = test_rooms.RoomAttributionTests.office
    record = test_rooms.RoomAttributionTests.record
    poll = test_rooms.RoomAttributionTests.poll

    async def test_opt_in_bounds_public_snapshot_without_deleting_human_evidence(self):
        self.office()
        starts = self.record(60)
        stored = deepcopy(self.runtime.data["room_labels"])
        with patch.object(seen_by, "SEEN_BY_HISTORY_HOURS", 0.5, create=True):
            await self.poll()
            rows = (await self.poll())["room_attribution"][self.zone]
        self.assertTrue(rows)
        self.assertGreaterEqual(rows[0]["start"], self.now - 1800)
        self.assertLess(len(rows), 50)
        self.assertEqual(rows[-1]["start"], starts[-1])
        self.assertEqual(self.runtime.data["room_labels"], stored)

    async def test_exact_default_and_policy_change_rebuild_public_snapshot(self):
        self.office()
        self.record(60)
        with patch.object(seen_by, "SEEN_BY_HISTORY_HOURS", None, create=True):
            await self.poll()
            exact = (await self.poll())["room_attribution"][self.zone]
        self.assertEqual(len(exact), 50)
        with patch.object(seen_by, "SEEN_BY_HISTORY_HOURS", 0.5, create=True):
            await self.poll()
            recent = (await self.poll())["room_attribution"][self.zone]
        self.assertLess(len(recent), len(exact))
        with patch.object(seen_by, "SEEN_BY_HISTORY_HOURS", None, create=True):
            await self.poll()
            restored = (await self.poll())["room_attribution"][self.zone]
        self.assertEqual(restored, exact)

    def test_learning_views_remain_independent_and_own_their_window(self):
        self.office()
        self.record(60)
        definitions = groups.definitions(self.runtime)[2]
        exact = attribution.prepared_view(self.runtime, "a")
        bounded = attribution.prepared_view(self.runtime, "a", since=self.now - 1800)
        self.assertNotEqual(exact._attribution_since, bounded._attribution_since)
        with (
            patch.object(seen_by, "SEEN_BY_HISTORY_HOURS", 0.5, create=True),
            patch.object(seen_by.time, "time", return_value=self.now),
        ):
            views = seen_by._view(self.runtime, definitions, self.zone)
            learning = attribution.prepared_view(self.runtime, "a")
        self.assertEqual(views["a"]._attribution_since, self.now - 1800)
        self.assertEqual(learning._attribution_since, 0)

    async def test_inflight_policy_change_rejects_old_display_result(self):
        self.office()
        self.record(60)
        real_build = seen_by.build

        def changed_policy(*args):
            result = real_build(*args)
            seen_by.SEEN_BY_HISTORY_HOURS = None
            return result

        with (
            patch.object(seen_by, "SEEN_BY_HISTORY_HOURS", 0.5),
            patch.object(seen_by, "build", side_effect=changed_policy),
        ):
            await self.poll()
            self.assertNotIn(self.zone, self.runtime._room_attribution)
        await self.poll()
        self.assertEqual(len((await self.poll())["room_attribution"][self.zone]), 50)

    async def test_recent_own_human_label_overrides_strong_energy_in_opt_in_mode(self):
        self.office()
        starts = self.record(60)
        device = self.runtime.data["devices"]["a"]
        device["history_labels"] = [
            {"start": starts[-1], "end": starts[-1] + 36, "state": "not_present"}
        ]
        device["label_revision"] += 1
        stored = deepcopy(device["history_labels"])
        with patch.object(seen_by, "SEEN_BY_HISTORY_HOURS", 0.5):
            await self.poll()
            rows = (await self.poll())["room_attribution"][self.zone]
        self.assertEqual(rows[-1]["devices"]["a"]["state"], "no")
        self.assertEqual(rows[-1]["devices"]["a"]["confidence"], 1.0)
        self.assertFalse(rows[-1]["devices"]["a"].get("inferred", False))
        self.assertEqual(device["history_labels"], stored)


def test_seen_by_attributes_recent_history_by_default():
    assert seen_by.SEEN_BY_HISTORY_HOURS is not None
    assert seen_by.SEEN_BY_HISTORY_HOURS <= 7 * 24
