"""Room replay and grouping boundaries use synthetic, independently labelled zones."""

import asyncio
import types
import unittest
from collections import Counter
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from unittest.mock import AsyncMock, patch

import room_scenes
import test_tuner as harness
from sample_rows import rows_of
from test_tuner import set_records as set_records
from tuner_under_test.rooms import assessment, evidence, groups, intervals, jobs, replay, truth


def member(device_id, values, labels, timing=None, offset=0):
    records = [(offset + i * 6, [value] + [255] * 17 + [0, 0]) for i, value in enumerate(values)]
    device = {"history_labels": labels, "training_state": "unknown"}
    view = harness.stub_view(device_id, device, records)
    return {"view": view, "thresholds": {"g0_move": 20}, "timing": timing or {}}


def labels(split=600, end=1200):
    return [
        {"start": 0, "end": split, "state": "present"},
        {"start": split, "end": end, "state": "not_present"},
    ]


class ReplayTests(unittest.TestCase):
    def test_complementary_radars_cover_each_other_on_same_clock(self):
        members = {
            "a": member("a", [40] * 50 + [0] * 150, labels()),
            "b": member("b", [0] * 50 + [40] * 50 + [0] * 100, labels()),
        }
        report = assessment.calculate(members, 0, 1200)
        self.assertGreater(report["room"]["presence_recall"], 0.99)
        self.assertLess(report["devices"]["a"]["presence_recall"], 0.51)
        self.assertLess(report["devices"]["b"]["presence_recall"], 0.51)
        self.assertGreater(report["devices"]["b"]["exclusive_presence_seconds"], 290)
        self.assertGreater(report["room"]["score"], report["devices"]["a"]["score"])

    def test_any_member_false_trigger_counts_and_overlap_is_not_added(self):
        a = member("a", [40] * 105 + [0] * 95, labels())
        one = assessment.calculate({"a": a}, 0, 1200)
        two = assessment.calculate(
            {"a": a, "b": member("b", [40] * 105 + [0] * 95, labels())}, 0, 1200
        )
        empty = two["room"]["outcomes"]["human"]["not_present"]
        # Five loud readings from 600 s, each held until the next: 600 to 630.
        self.assertEqual(empty["active_seconds"], 30)
        self.assertEqual(
            empty["active_seconds"],
            one["room"]["outcomes"]["human"]["not_present"]["active_seconds"],
        )
        self.assertEqual(two["devices"]["a"]["exclusive_presence_seconds"], 0)

    def test_office_zones_are_independent_of_whole_area_occupancy(self):
        a = member("a", [40] * 100 + [0] * 100, labels())
        b_labels = [{"start": 0, "end": 1200, "state": "not_present"}]
        b = member("b", [0] * 200, b_labels)
        area = assessment.calculate({"a": a, "b": b}, 0, 1200)
        zone = assessment.calculate({"b": b}, 0, 1200)
        self.assertGreater(area["room"]["outcomes"]["human"]["present"]["seconds"], 590)
        self.assertEqual(zone["room"]["outcomes"]["human"]["present"]["seconds"], 0)
        self.assertEqual(zone["room"]["outcomes"]["human"]["not_present"]["active_seconds"], 0)
        # A broken occupied zone stays broken even while the neighbouring zone detects.
        b["view"].data["devices"]["b"]["history_labels"] = labels()
        broken = assessment.calculate({"b": b}, 0, 1200)
        self.assertEqual(broken["room"]["presence_recall"], 0)

    def test_missing_recordings_are_excluded_not_called_empty(self):
        a = member("a", [40] * 100 + [0] * 100, labels())
        b = member("b", [0] * 100, labels(), offset=600)
        report = assessment.calculate({"a": a, "b": b}, 0, 1200)
        # b holds 600 to 1197 (its last reading half a sample longer, as recorded).
        self.assertEqual(report["shared_recording_seconds"], 597)
        self.assertIsNone(report["room"]["score"])
        self.assertEqual(report["excluded"]["no_shared_recording_seconds"], 603)

    def test_incomplete_sample_breaks_coverage(self):
        values = [0] * 200
        values[100] = 255
        report = assessment.calculate({"a": member("a", values, labels())}, 0, 1200)
        # 0 to 1197 recorded, less the 6 s the unread reading held.
        self.assertEqual(report["shared_recording_seconds"], 1191)

    def test_per_radar_timing_precedes_union(self):
        rows_a = [(i, {"g0_move": 40 if i < 5 else 0}, "unknown") for i in range(30)]
        rows_b = [(i, {"g0_move": 40 if 8 <= i < 12 else 0}, "unknown") for i in range(30)]
        timing_a = {"timeout": 10, "on_delay": 0, "off_delay": 0}
        timing_b = {"timeout": 1, "on_delay": 0, "off_delay": 0}
        a = replay.radar(rows_of(rows_a), {"g0_move": 20}, timing_a)
        b = replay.radar(rows_of(rows_b), {"g0_move": 20}, timing_b)
        combined = replay.lookups(replay.combined({"a": a, "b": b}))
        self.assertTrue(combined["lower"].at(13))  # a's hold still covers the handoff
        self.assertFalse(replay.lookups(b)["lower"].at(13, False))
        self.assertFalse(combined["lower"].at(20, False))

    def test_label_priority_and_unknown_coverage(self):
        automatic = intervals.Timeline([(0, 100, ("present", "automatic", 0.8))])
        a = {"manual": lambda t: "not_present", "automatic": automatic}
        b = {"manual": lambda t: None, "automatic": intervals.Timeline([])}
        self.assertEqual(evidence.device_label(a, 10), ("not_present", "human", 1))
        self.assertIsNone(room_scenes.room_label({"a": a, "b": b}, 10))
        a["manual"] = lambda t: "unknown"
        self.assertIsNone(evidence.device_label(a, 10))
        a["manual"] = lambda t: "present"
        self.assertEqual(room_scenes.room_label({"a": a, "b": b}, 10), ("present", "human", 1))

    def test_automatic_scores_identify_estimated_evidence(self):
        records = [
            (i * 6, [40 if i < 100 else 0] + [255] * 17 + [1 if i < 100 else 2, 100])
            for i in range(200)
        ]
        a = member("a", [40] * 100 + [0] * 100, [])
        a["view"] = harness.stub_view(
            "a", {"history_labels": [], "training_state": "unknown"}, records
        )
        report = assessment.calculate({"a": a}, 0, 1200)
        self.assertEqual(report["room"]["basis"], "estimated")
        self.assertGreater(report["room"]["score"], 99)

    def test_asynchronous_timestamps_intersect_actual_coverage(self):
        report = assessment.calculate(
            {
                "a": member("a", [40] * 100 + [0] * 100, labels()),
                "b": member("b", [40] * 100 + [0] * 100, labels(), offset=3),
            },
            0,
            1200,
        )
        # a holds 0 to 1197, b 3 to 1200: both recorded from 3 to 1197.
        self.assertEqual(report["shared_recording_seconds"], 1194)
        self.assertLessEqual(report["room"]["presence_recall"], 1)


def role(report, key):
    return {
        name: report["devices"][key][name]
        for name in (
            "missed_seen_seconds",
            "missed_seen_inferred_seconds",
            "missed_seen_automatic_seconds",
            "false_active_seconds",
            "occupied_seconds",
            "empty_seconds",
        )
    }


class RoleTests(unittest.TestCase):
    def test_missed_seen_counts_only_attributed_time(self):
        # a is labelled Present and detects only the first half; b is labelled Empty
        # throughout, so the room is Present by a's label but b is not attributed.
        a = member("a", [40] * 50 + [0] * 150, [{"start": 0, "end": 600, "state": "present"}])
        b = member("b", [0] * 200, [{"start": 0, "end": 1200, "state": "not_present"}])
        report = assessment.calculate({"a": a, "b": b}, 0, 1200)
        self.assertAlmostEqual(role(report, "a")["missed_seen_seconds"], 297, delta=6)
        self.assertEqual(role(report, "a")["missed_seen_inferred_seconds"], 0)
        self.assertEqual(role(report, "b")["missed_seen_seconds"], 0)
        self.assertAlmostEqual(role(report, "a")["occupied_seconds"], 597, delta=6)
        self.assertEqual(
            role(report, "a")["occupied_seconds"], role(report, "b")["occupied_seconds"]
        )

    def test_false_active_seconds_per_member(self):
        a = member("a", [40] * 105 + [0] * 95, labels())
        b = member("b", [0] * 200, labels())
        report = assessment.calculate({"a": a, "b": b}, 0, 1200)
        # Readings held until the next (the last half a sample): loud 600 to 630, empty
        # labelled time recorded from 600 to 1197.
        self.assertEqual(role(report, "a")["false_active_seconds"], 30)
        self.assertEqual(role(report, "b")["false_active_seconds"], 0)
        self.assertEqual(role(report, "a")["empty_seconds"], 597)
        self.assertEqual(role(report, "b")["empty_seconds"], 597)

    def test_missed_seen_inferred_from_baseline_is_separate(self):
        # b is labelled only while Empty: that baseline makes its energy during the occupied
        # half a rule-2 "yes", even though its threshold never fires.
        a = member("a", [40] * 100 + [0] * 100, labels())
        values = [40] * 30 + [0] * 70 + [0] * 100
        b = member("b", values, [{"start": 600, "end": 1200, "state": "not_present"}])
        b["thresholds"] = {"g0_move": 100}  # never detects, though the energy was seen
        report = assessment.calculate({"a": a, "b": b}, 0, 1200)
        found = role(report, "b")
        self.assertGreater(found["missed_seen_seconds"], 0)
        self.assertEqual(found["missed_seen_inferred_seconds"], found["missed_seen_seconds"])

    def test_inferred_yes_below_the_confidence_floor_is_not_counted(self):
        a = member("a", [40] * 100 + [0] * 100, labels())
        b = member(
            "b", [40] * 30 + [0] * 170, [{"start": 900, "end": 1200, "state": "not_present"}]
        )
        b["thresholds"] = {"g0_move": 100}  # 50 baseline samples: confidence 0.5 < 0.55
        found = role(assessment.calculate({"a": a, "b": b}, 0, 1200), "b")
        self.assertEqual(found["missed_seen_seconds"], 0)
        self.assertEqual(found["missed_seen_inferred_seconds"], 0)

    def test_room_label_snapshot_attributes_a_radar_with_no_labels(self):
        a = member("a", [40] * 50 + [0] * 150, [])
        room = {
            "group_id": "g",
            "groups": {"g": {"kind": "zone", "device_ids": ["a"]}},
            "room_labels": {
                "g": [
                    {"start": 0, "end": 600, "state": "present", "source": "manual"},
                    {"start": 600, "end": 1200, "state": "not_present", "source": "manual"},
                ]
            },
        }
        found = role(assessment.calculate({"a": a}, 0, 1200, room), "a")
        self.assertAlmostEqual(found["missed_seen_seconds"], 297, delta=6)
        self.assertEqual(found["missed_seen_inferred_seconds"], found["missed_seen_seconds"])
        self.assertEqual(found["missed_seen_automatic_seconds"], 0)

    def test_sensor_only_room_counts_missed_seen_as_automatic(self):
        a = member("a", [40] * 100 + [0] * 100, [])
        a["thresholds"] = {"g0_move": 100}  # never detects what it saw
        room = {
            "group_id": "g",
            "groups": {"g": {"kind": "zone", "device_ids": ["a"]}},
            "room_labels": {
                "g": [
                    {"start": 0, "end": 600, "state": "present", "source": "sensor"},
                    {"start": 600, "end": 1200, "state": "not_present", "source": "sensor"},
                ]
            },
        }
        found = role(assessment.calculate({"a": a}, 0, 1200, room), "a")
        self.assertAlmostEqual(found["missed_seen_seconds"], 597, delta=6)
        self.assertEqual(found["missed_seen_automatic_seconds"], found["missed_seen_seconds"])
        self.assertEqual(found["missed_seen_inferred_seconds"], 0)

    def test_unlabelled_room_reports_zero_role_seconds(self):
        report = assessment.calculate({"a": member("a", [0] * 200, [])}, 0, 1200)
        self.assertEqual(
            role(report, "a"),
            dict.fromkeys(role(report, "a"), 0.0),
        )


class GroupTests(unittest.IsolatedAsyncioTestCase):
    setUp = harness.RuntimeTests.setUp
    asyncTearDown = harness.RuntimeTests.asyncTearDown

    def office(self):
        self.runtime.data["devices"]["b"] = deepcopy(self.device)
        areas = types.SimpleNamespace(
            async_list_areas=lambda: [types.SimpleNamespace(id="office", name="Office")]
        )
        registry = types.SimpleNamespace(
            async_get=lambda key: types.SimpleNamespace(
                name_by_user=None, name=key, area_id="office"
            )
        )
        for module, value in ((groups.ar, areas), (groups.dr, registry)):
            patcher = patch.object(module, "async_get", return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.runtime._threshold_configuration = lambda key: (
            {"g0_move": f"number.{key}"},
            {"g0_move": 20},
        )
        self.runtime._history_view = lambda key: member(key, [40] * 100 + [0] * 100, labels())[
            "view"
        ]

    def test_area_defaults_and_manual_zones_preserve_membership(self):
        self.office()
        self.assertEqual(
            groups.definitions(self.runtime)[2]["area:office"]["device_ids"], ["a", "b"]
        )
        zone = groups.configure(self.runtime, None, "Desk", "office", ["a", "a"])
        other = groups.configure(self.runtime, None, "Sofa", "office", ["b"])
        definitions = groups.definitions(self.runtime)[2]
        self.assertEqual(definitions[zone["id"]]["device_ids"], ["a"])
        self.assertEqual(definitions[other["id"]]["device_ids"], ["b"])
        self.assertEqual(definitions["area:office"]["device_ids"], ["a", "b"])
        groups.configure(self.runtime, "area:office", "Office", "office", ["b"])
        self.assertEqual(groups.definitions(self.runtime)[2]["area:office"]["device_ids"], ["b"])
        groups.remove(self.runtime, "area:office")
        self.assertEqual(
            groups.definitions(self.runtime)[2]["area:office"]["device_ids"], ["a", "b"]
        )
        self.assertIn(zone["id"], groups.definitions(self.runtime)[2])

    def test_snapshot_layout_places_every_snapshot_device(self):
        self.office()
        for key in ("a", "b"):
            self.registry.entities[f"sensor.{key}_g0_move_energy"] = types.SimpleNamespace(
                entity_id=f"sensor.{key}_g0_move_energy", device_id=key, domain="sensor"
            )
        snapshot = self.runtime.snapshot()
        self.assertEqual(set(snapshot["devices"]), {"a", "b"})
        office = next(n for n in snapshot["layout"] if n["id"] == "area:office")
        self.assertEqual(office["devices"], ["a", "b"])
        placed = {d for n in snapshot["layout"] for d in n["devices"]}
        self.assertEqual(placed, set(snapshot["devices"]))
        self.assertIn("area:office", snapshot["rooms"]["groups"])

    def test_paused_devices_excluded_from_defaults_but_manually_selectable(self):
        self.office()
        self.runtime.data["devices"]["b"]["recording_enabled"] = False
        self.assertEqual(groups.definitions(self.runtime)[2]["area:office"]["device_ids"], ["a"])
        self.assertEqual(
            groups.configure(self.runtime, None, "Historic", None, ["b"])["device_ids"], ["b"]
        )

    def test_group_validation(self):
        self.office()
        for name, area, members in (
            ("", None, ["a"]),
            ("Zone", "missing", ["a"]),
            ("Zone", None, []),
            ("Zone", None, ["unknown"]),
        ):
            with self.subTest(name=name, area=area, members=members), self.assertRaises(ValueError):
                groups.configure(self.runtime, None, name, area, members)

    async def test_jobs_survive_request_return_cache_and_detect_changed_inputs(self):
        self.office()
        with patch.object(jobs.time, "time", return_value=1200):
            self.assertEqual(jobs.start(self.runtime, "area:office", 1)["state"], "running")
            task = self.runtime._room_jobs["area:office"]
            jobs.start(self.runtime, "area:office", 1)
            self.assertIs(task, self.runtime._room_jobs["area:office"])
            await task
        self.assertEqual(jobs.summary(self.runtime, "area:office")["state"], "ready")
        with patch.object(assessment, "calculate", side_effect=AssertionError("Must use cache")):
            for _ in range(3):
                self.assertEqual(
                    jobs.snapshot_with_inventory(self.runtime)[0]["groups"]["area:office"][
                        "assessment"
                    ]["state"],
                    "ready",
                )
        with patch.object(assessment, "SCORER_VERSION", 3):
            self.assertEqual(jobs.summary(self.runtime, "area:office")["state"], "stale")
        self.device["label_revision"] = 1
        self.assertEqual(jobs.summary(self.runtime, "area:office")["state"], "stale")

    async def test_errors_are_visible_and_invalid_windows_rejected(self):
        self.office()
        for hours in (float("nan"), 0, 169, True):
            with self.assertRaises(ValueError):
                jobs.start(self.runtime, "area:office", hours)
        with patch.object(assessment, "calculate", side_effect=ValueError("Synthetic failure")):
            jobs.start(self.runtime, "area:office", 1)
            await self.runtime._room_jobs["area:office"]
        self.assertEqual(jobs.summary(self.runtime, "area:office")["reason"], "Synthetic failure")

    async def test_edit_during_job_never_attaches_result_to_new_members(self):
        self.office()
        started, finish = asyncio.Event(), asyncio.Event()

        async def executor(*args):
            started.set()
            await finish.wait()
            return {"room": {"score": 100}}

        self.hass.async_add_executor_job = executor
        jobs.start(self.runtime, "area:office", 1)
        task = self.runtime._room_jobs["area:office"]
        await started.wait()
        groups.configure(self.runtime, "area:office", "Office", "office", ["b"])
        finish.set()
        await task
        report = jobs.summary(self.runtime, "area:office")
        self.assertEqual(report["state"], "stale")
        self.assertNotIn("room", report)

    async def test_unload_cancels_assessment_without_saving_partial_result(self):
        from tuner_under_test.runtime import schedule

        self.office()
        started = asyncio.Event()

        async def executor(*args):
            started.set()
            await asyncio.Event().wait()

        self.hass.async_add_executor_job = executor
        jobs.start(self.runtime, "area:office", 1)
        task = self.runtime._room_jobs["area:office"]
        await started.wait()
        await schedule.stop(self.runtime)
        self.assertTrue(task.cancelled())
        self.assertEqual(jobs.summary(self.runtime, "area:office"), {"state": "pending"})


class RoomAttributionTests(unittest.IsolatedAsyncioTestCase):
    """``snapshot["room_attribution"]``: who saw each occupied episode, cached off-loop."""

    def setUp(self):
        harness.RuntimeTests.setUp(self)
        self.now = 2_000_000_000.0
        self.read_time = self.now
        import time

        clock = patch.object(time, "time", side_effect=lambda: self.read_time)
        clock.start()
        self.addCleanup(clock.stop)

    asyncTearDown = harness.RuntimeTests.asyncTearDown

    def office(self):
        from tuner_under_test.rooms import seen_by

        GroupTests.office(self)
        del self.runtime._history_view  # Seen by reads real history views, like learning.
        self.clock = 1000.0
        clock = patch.object(seen_by, "_clock", side_effect=lambda: self.clock)
        clock.start()
        self.addCleanup(clock.stop)
        for key in ("a", "b"):
            self.registry.entities[f"sensor.{key}_g0_move_energy"] = types.SimpleNamespace(
                entity_id=f"sensor.{key}_g0_move_energy", device_id=key, domain="sensor"
            )

    def record(self, episodes, zone=None, flush=True, quiet_b=False):
        """Radar a: per episode 6 strong samples with the zone Present, then 6 quiet ones with
        the area Empty (which empties the zone too, so a is quiet in every group it is in)."""
        self.zone = zone or groups.configure(self.runtime, None, "Desk", "office", ["a"])["id"]
        first = self.now - episodes * 72 - 60
        starts, saved, empty = [], [], []
        for index in range(episodes):
            begin = first + index * 72
            starts.append(begin)
            for step in range(12):
                value = 60 if step < 6 else 2
                self.runtime._record_history_sample("a", {"g0_move": value}, begin + step * 6)
                if quiet_b:  # b, offset 3 s, stays quiet throughout.
                    self.runtime._record_history_sample("b", {"g0_move": 2}, begin + step * 6 + 3)
            saved.append(
                {"start": begin, "end": begin + 36, "state": "present", "source": "manual"}
            )
            empty.append(
                {"start": begin + 36, "end": begin + 72, "state": "not_present", "source": "manual"}
            )
        self.runtime.data["room_labels"] = {self.zone: saved, "area:office": empty}
        self.runtime.data["room_label_revision"] = {self.zone: 1, "area:office": 1}
        return starts

    async def poll(self):
        snapshot = self.runtime.snapshot()
        await asyncio.gather(*self.runtime._room_attribution_jobs.values())
        return snapshot

    def built(self, spy):
        return sorted(call.args[2] for call in spy.call_args_list)

    @contextmanager
    def native_builds(self):
        """Count actual physical attribution passes for each memoizable job key."""
        from tuner_under_test.rooms import event_attribution

        counts, held = Counter(), []
        original = event_attribution._cached
        with patch.object(event_attribution, "_found", wraps=event_attribution._found) as found:

            def cached(view, key, *args):
                held.append(view)  # Prevent object-id reuse between completed jobs.
                before = found.call_count
                result = original(view, key, *args)
                if key[2]:
                    counts[id(view), key] += found.call_count - before
                return result

            with patch.object(event_attribution, "_cached", side_effect=cached):
                yield counts

    def assert_native_builds(self, counts, devices):
        self.assertTrue(counts)
        self.assertEqual({key[1][0] for key in counts}, set(devices))
        self.assertEqual(set(counts.values()), {1}, "one physical fold per job and truth kind")

    async def test_room_attribution_bounded_to_50(self):
        self.office()
        starts = self.record(60)
        first = await self.poll()
        self.assertEqual(first["room_attribution"][self.zone], [], "computed off the poll")
        snapshot = await self.poll()
        episodes = snapshot["room_attribution"][self.zone]
        self.assertEqual(len(episodes), 50)
        self.assertEqual([item["start"] for item in episodes], starts[10:])
        # The end covers the last Present sample's 6 s cell.
        self.assertEqual(episodes[-1]["end"], starts[-1] + 36)
        self.assertEqual(
            episodes[-1]["devices"], {"a": {"state": "yes", "confidence": 1.0, "inferred": True}}
        )
        # A zone Present implies its area Present; b recorded nothing, so it is unknown.
        area = snapshot["room_attribution"]["area:office"]
        self.assertEqual(len(area), 50)
        self.assertEqual(
            area[-1]["devices"],
            {
                "a": {"state": "yes", "confidence": 1.0, "inferred": True},
                "b": {"state": "unknown", "confidence": 0.0, "inferred": True},
            },
        )

    async def test_room_attribution_is_cached_until_its_inputs_change(self):
        from tuner_under_test.rooms import room_labels, seen_by

        self.office()
        starts = self.record(3)
        with patch.object(seen_by, "build", wraps=seen_by.build) as spy:
            for _ in range(3):
                await self.poll()
            self.assertEqual(self.built(spy), ["area:office", self.zone], "one build per group")
            before = self.runtime.snapshot()["room_attribution"][self.zone]
            room_labels.label_room_range(
                self.runtime, self.zone, starts[0], starts[0] + 6, "unknown"
            )
            stale = self.runtime.snapshot()["room_attribution"][self.zone]
            self.assertEqual((len(stale), stale), (3, before), "served while it rebuilds")
            await asyncio.gather(*self.runtime._room_attribution_jobs.values())
            self.assertEqual(len(spy.call_args_list), 4, "a room label edit rebuilds zone and area")
            self.runtime.data["devices"]["a"]["label_revision"] = 7
            await self.poll()
            self.assertEqual(len(spy.call_args_list), 6, "a member label edit rebuilds")
            self.runtime._record_history_sample("a", {"g0_move": 3}, self.now)
            await self.poll()
            self.assertEqual(len(spy.call_args_list), 6, "pending samples wait for their block")
            await self.poll()
            self.assertEqual(len(spy.call_args_list), 6, "history-only changes wait")
            self.clock += seen_by.HISTORY_REBUILD_SECONDS - 1
            await self.poll()
            self.assertEqual(len(spy.call_args_list), 6)
            self.clock += 1
            await self.poll()
            self.assertEqual(len(spy.call_args_list), 8, "a new history block rebuilds in time")
            await self.poll()
            self.assertEqual(len(spy.call_args_list), 8)
            self.runtime._record_history_sample("a", {"g0_move": 2}, self.now + 6)
            self.runtime.data["devices"]["a"]["label_revision"] = 8
            await self.poll()
            self.assertEqual(len(spy.call_args_list), 10, "a label change never waits")

    async def test_a_shared_radar_is_attributed_once_for_all_the_groups_of_a_poll(self):
        """Regression: a poll that finds several groups stale prepared every group's views before
        any build had landed, so a radar in three groups was attributed three times in a row."""
        self.office()
        self.record(3, quiet_b=True)
        with self.native_builds() as builds:
            await self.poll()
        self.assert_native_builds(builds, "ab")
        episodes = (await self.poll())["room_attribution"]
        self.assertEqual(len(episodes[self.zone]), 3)
        self.assertEqual(len(episodes["area:office"]), 3)

    async def test_queued_jobs_each_reuse_their_own_frozen_attribution(self):
        """Two jobs own separate frozen reads, and repeated reads inside each fold once."""
        from tuner_under_test.rooms import attribution, event_attribution

        self.office()
        self.record(3, quiet_b=True)
        first, second = (attribution.prepared_view(self.runtime, "a") for _ in range(2))

        def job(view):
            one = attribution.radar_attribution(view, "a", view.data["room_snapshot"])["found"]
            two = attribution.radar_attribution(view, "a", view.data["room_snapshot"])["found"]
            self.assertIs(one, two)
            return one

        with patch.object(event_attribution, "_found", wraps=event_attribution._found) as spy:
            found = await jobs.on_views(self.runtime, {"g": {"a": {"view": first}}}, job, first)
            again = await jobs.on_views(self.runtime, {"g": {"a": {"view": second}}}, job, second)
        self.assertEqual(spy.call_count, 2, "each frozen job computes once")
        self.assertEqual(again, found)

    async def test_every_input_of_a_radars_baseline_is_in_the_signature(self):
        from tuner_under_test.rooms import seen_by

        self.office()
        self.record(2)
        # a is also in an area-less group: its labels change a's quiet samples, so the zone.
        other = groups.configure(self.runtime, None, "Bench", None, ["a"])["id"]
        await self.poll()
        changes = (
            lambda: self.runtime.data.setdefault("room_label_revision", {}).update({other: 1}),
            lambda: self.runtime.data.setdefault("room_sensor_revision", {}).update({self.zone: 1}),
            lambda: self.runtime.data["devices"]["a"].update(training_label_start=self.now),
        )
        for change in changes:
            with (
                self.subTest(change=change),
                patch.object(seen_by, "build", wraps=seen_by.build) as spy,
            ):
                change()
                await self.poll()
                self.assertIn(self.zone, self.built(spy))

    async def test_history_fingerprint_sees_flushes_and_prunes(self):
        from tuner_under_test.history import ingest
        from tuner_under_test.rooms import seen_by

        self.office()
        self.record(6)
        store = self.runtime._days
        ingest.end(self.runtime, "a", self.now)
        before = seen_by.signature(self.runtime, groups.definitions(self.runtime)[2], self.zone)
        store.flush(self.now + 86400)
        after = seen_by.signature(self.runtime, groups.definitions(self.runtime)[2], self.zone)
        self.assertNotEqual(before["history"], after["history"], "finishing a day changes its mark")
        date = store.days("a")[0]
        await self.poll()
        for edge in ("first_start", "first_end", "last_end", "delete"):
            with (
                self.subTest(edge=edge),
                patch.object(seen_by, "build", wraps=seen_by.build) as spy,
            ):
                if edge == "delete":
                    store.delete("a", [date])
                else:
                    day = deepcopy(store.day("a", date))
                    if edge == "first_start":
                        day.times[0] += 1
                    elif edge == "first_end":
                        day.ends[0] -= 1
                    else:
                        day.ends[-1] -= 1
                    store.replace("a", date, day)
                self.clock += seen_by.HISTORY_REBUILD_SECONDS
                await self.poll()
                self.assertIn(self.zone, self.built(spy))

    async def test_builds_run_one_at_a_time(self):
        self.office()
        self.record(2)
        active, peak = 0, 0

        async def executor(fn, *args):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
            return fn(*args)

        self.hass.async_add_executor_job = executor
        await self.poll()
        self.assertEqual(len(self.runtime._room_attribution), 2)
        self.assertEqual(peak, 1)

    def guess(self, t, state, confidence=1.0):
        self.runtime.data["devices"]["a"].setdefault("auto", {})["last_classification"] = {
            "state": state,
            "confidence": confidence,
            "timestamp": t,
        }

    async def test_estimate_only_episodes_are_marked(self):
        self.office()
        starts = self.record(2)
        later = starts[-1] + 144
        for step in range(12):
            self.guess(later + step * 6, "present" if step < 6 else "not_present")
            self.runtime._record_history_sample("a", {"g0_move": 60}, later + step * 6)
        self.read_time = later + 72
        await self.poll()
        episodes = (await self.poll())["room_attribution"][self.zone]
        self.assertEqual([item["start"] for item in episodes], [*starts, later])
        self.assertEqual([item["estimated"] for item in episodes], [False, False, True])

    def test_snapshot_publishes_the_current_room_estimate(self):
        from tuner_under_test.rooms import room_labels

        self.office()
        zone = groups.configure(self.runtime, None, "Desk", "office", ["a"])["id"]
        first = self.now - 60
        for step in range(10):
            self.guess(first + step * 6, "present")
            self.runtime._record_history_sample("a", {"g0_move": 60}, first + step * 6)
        with patch.object(room_labels.time, "time", return_value=self.now):
            estimate = jobs.snapshot_with_inventory(self.runtime)[0]["groups"][zone]["estimate"]
            result = room_labels.room_estimate_feedback(self.runtime, zone, False)
        self.assertEqual(
            estimate,
            {"state": "present", "confidence": 0.95, "start": first, "end": self.now},
        )
        self.assertEqual(
            result["room_labels"],
            [{"start": first, "end": self.now, "state": "not_present", "source": "manual"}],
        )
        from tuner_under_test.history import ingest

        ingest.end(self.runtime, "a", self.now)
        with patch.object(room_labels.time, "time", return_value=self.now + 60):
            self.assertIsNone(
                jobs.snapshot_with_inventory(self.runtime)[0]["groups"][zone]["estimate"]
            )

    def test_the_published_estimate_is_cached_between_polls(self):
        from tuner_under_test.rooms import room_labels

        self.office()
        zone = groups.configure(self.runtime, None, "Desk", "office", ["a"])["id"]
        first = self.now - 60
        for step in range(10):
            self.guess(first + step * 6, "present")
            self.runtime._record_history_sample("a", {"g0_move": 60}, first + step * 6)
        estimates = room_labels.current_estimate
        with (
            patch.object(room_labels, "current_estimate", wraps=estimates) as spy,
            patch.object(room_labels.time, "time", return_value=self.now),
        ):
            first_poll = jobs.snapshot_with_inventory(self.runtime)[0]["groups"][zone]["estimate"]
            calls = spy.call_count
            again = jobs.snapshot_with_inventory(self.runtime)[0]["groups"][zone]["estimate"]
            self.assertEqual(spy.call_count, calls, "an unchanged poll reuses the estimate")
            self.assertEqual(again, first_poll)
            self.guess(self.now, "present")
            self.runtime._record_history_sample("a", {"g0_move": 61}, self.now)
            jobs.snapshot_with_inventory(self.runtime)[0]
            self.assertGreater(spy.call_count, calls, "a new sample recomputes it")
        calls = spy.call_count
        with (
            patch.object(room_labels, "current_estimate", wraps=estimates) as spy,
            patch.object(
                room_labels.time,
                "time",
                return_value=self.now + room_labels.ESTIMATE_CACHE_SECONDS,
            ),
        ):
            jobs.snapshot_with_inventory(self.runtime)[0]
            self.assertGreater(spy.call_count, 0, "time passing recomputes it (staleness)")

    async def test_seen_by_matches_the_learners_attribution(self):
        """Human-labelled episodes are attributed against the human-only baseline, as
        ``room_targets`` (the learner) does; a full-truth baseline would differ here."""
        from tuner_under_test.rooms import attribution, seen_by

        self.office()
        self.zone = groups.configure(self.runtime, None, "Desk", "office", ["a"])["id"]
        first = self.now - 3600
        t, zone, area = first, [], []
        # Human Empty (area Empty empties the zone): a quiet at 2, 60 samples.
        area.append({"start": t, "end": t + 360, "state": "not_present", "source": "manual"})
        for step in range(60):
            self.runtime._record_history_sample("a", {"g0_move": 2}, t + step * 6)
        t += 400
        # No human label, both radars guess Empty: the estimate is Empty, a reads 40.
        for step in range(80):
            for key in ("a", "b"):
                self.runtime.data["devices"][key].setdefault("auto", {})["last_classification"] = {
                    "state": "not_present",
                    "confidence": 0.9,
                    "timestamp": t + step * 6,
                }
                self.runtime._record_history_sample(key, {"g0_move": 40}, t + step * 6)
        for key in ("a", "b"):
            self.runtime.data["devices"][key]["auto"]["last_classification"] = {}
        t += 600
        # Human Present on the zone: a reads 30, above its human baseline only.
        zone.append({"start": t, "end": t + 60, "state": "present", "source": "manual"})
        for step in range(10):
            self.runtime._record_history_sample("a", {"g0_move": 30}, t + step * 6)
        self.runtime.data["room_labels"] = {self.zone: zone, "area:office": area}
        self.runtime.data["room_label_revision"] = {self.zone: 1, "area:office": 1}
        with patch.object(
            attribution, "radar_attribution", wraps=attribution.radar_attribution
        ) as spy:
            await self.poll()
            episodes = (await self.poll())["room_attribution"][self.zone]
            self.assertTrue(spy.called, "Seen by uses the learner's attribution")
        chip = episodes[-1]["devices"]["a"]
        saved = attribution.snapshot(self.runtime, "a")
        learner = attribution.radar_attribution(self.runtime, "a", saved)["found"][self.zone]
        self.assertEqual(chip, learner[-1][1])
        self.assertEqual((chip["state"], chip["inferred"]), ("yes", True))
        full = attribution.radar_attribution(self.runtime, "a", saved, human=False)
        self.assertEqual(full["found"][self.zone][-1][1]["state"], "no", "the baselines differ")
        with patch.object(
            attribution, "radar_attribution", wraps=attribution.radar_attribution
        ) as spy:
            attribution.room_targets(self.runtime, "a")
            self.assertTrue(spy.called, "the learner path uses the same function")
        self.assertIn(seen_by.build.__module__, seen_by.__name__)

    def shared_radar_desk(self):
        """Radar a is in the office area [a, b] and the Desk zone [a]. It reads 40 while Desk
        is Empty but the area is Present (someone elsewhere in the office), 2 while the area
        is Empty, then 30 under human Desk Present. Only the union rule (quiet when every
        group is Empty) keeps the 40s out of a's baseline, so only it attributes the 30s."""
        self.office()
        desk = groups.configure(self.runtime, None, "Desk", "office", ["a"])["id"]
        t, zone, area = self.now - 3600, [], []
        zone.append({"start": t, "end": t + 360, "state": "not_present", "source": "manual"})
        area.append({"start": t, "end": t + 360, "state": "present", "source": "manual"})
        for step in range(60):
            self.runtime._record_history_sample("a", {"g0_move": 40}, t + step * 6)
        t += 400
        area.append({"start": t, "end": t + 360, "state": "not_present", "source": "manual"})
        for step in range(60):
            self.runtime._record_history_sample("a", {"g0_move": 2}, t + step * 6)
        t += 400
        zone.append({"start": t, "end": t + 60, "state": "present", "source": "manual"})
        for step in range(10):
            self.runtime._record_history_sample("a", {"g0_move": 30}, t + step * 6)
        self.runtime.data["room_labels"] = {desk: zone, "area:office": area}
        self.runtime.data["room_label_revision"] = {desk: 1, "area:office": 1}
        members = {
            "a": {
                "view": self.runtime._history_view("a"),
                "thresholds": {"g0_move": 100},  # Never detects what it saw.
                "timing": {},
            }
        }
        return desk, t, members, truth.view(self.runtime, desk)

    async def test_objective_and_roles_attribute_a_shared_radar_as_the_learner_does(self):
        """M2: assessment roles and the joint objective read the learner's attribution (one
        baseline, union rule over every group the radar is in), not a per-group baseline."""
        from tuner_under_test.rooms import attribution
        from tuner_under_test.rooms.objective import Objective

        desk, episode, members, room = self.shared_radar_desk()
        saved = attribution.snapshot(self.runtime, "a")
        learner = attribution.radar_attribution(self.runtime, "a", saved)["found"][desk][-1][1]
        self.assertEqual((learner["state"], learner["inferred"]), ("yes", True))
        self.assertEqual(
            attribution.room_targets(self.runtime, "a")(episode)[:2], ("present", "attributed")
        )
        start, end = episode - 1200, episode + 60
        report = assessment.calculate(members, start, end, room)
        role = report["devices"]["a"]
        self.assertGreater(role["missed_seen_seconds"], 0, "the learner's yes is a miss here")
        self.assertEqual(role["missed_seen_inferred_seconds"], role["missed_seen_seconds"])
        preference = Objective(members, start, end, room).preferences["a"]["g0_move"]
        self.assertEqual(preference["presence_reference"], 30, "attributed Present is evidence")

    async def test_a_job_attributes_each_radar_once(self):
        """Roles and measurements share each native human/full fold within the job."""

        desk, episode, _members, _room = self.shared_radar_desk()
        with (
            patch.object(jobs.time, "time", return_value=episode + 60),
            self.native_builds() as builds,
        ):
            jobs.start(self.runtime, desk, 1)
            await self.runtime._room_jobs[desk]
        self.assertEqual(jobs.summary(self.runtime, desk)["state"], "ready")
        self.assert_native_builds(builds, "a")
        self.assertGreater(jobs.summary(self.runtime, desk)["devices"]["a"]["occupied_seconds"], 0)
        self.assertFalse(
            getattr(self.runtime, "_native_found_job", {}), "native results never outlive a job"
        )

    async def test_a_job_cache_hit_decodes_no_history_and_release_drops_its_times(self):
        """A native memo hit reuses physical folds; release frees their column times."""
        from tuner_under_test.rooms import attribution, event_attribution

        desk, episode, _members, _room = self.shared_radar_desk()
        self.read_time = episode + 60
        view = attribution.prepared_view(self.runtime, "a")
        first = attribution.radar_attribution(view, "a", view.data["room_snapshot"])
        with patch.object(event_attribution, "_found", wraps=event_attribution._found) as spy:
            again = attribution.radar_attribution(view, "a", view.data["room_snapshot"])
        self.assertNotIn("rows", first)
        self.assertTrue(all(type(t) is float for t in first["times"]))
        self.assertGreater(len(first["times"]), 0)
        self.assertLess(len(first["times"]), 130, "change-only columns avoid repeated sample rows")
        self.assertEqual(spy.call_args_list, [], "a job memo hit folds no physical history")
        self.assertIs(again["found"], first["found"])
        self.assertTrue(first["found"][desk])
        attribution.release([view])
        self.assertFalse(view._native_found_job)

    async def test_unchanged_seen_by_reuses_job_folds_and_label_edits_get_a_new_job(self):
        from tuner_under_test.rooms import attribution, event_attribution, seen_by

        desk, _episode, _members, _room = self.shared_radar_desk()
        first = attribution.prepared_view(self.runtime, "a")
        expected = seen_by._member_episodes(first, "a", desk)
        with patch.object(event_attribution, "_found", wraps=event_attribution._found) as reads:
            self.assertEqual(seen_by._member_episodes(first, "a", desk), expected)
        self.assertEqual(reads.call_count, 0, "repeated job reads reuse human and full folds")
        full = attribution.radar_attribution(first, "a", first.data["room_snapshot"], human=False)
        oracle_view = self.runtime._history_view("a")
        oracle = attribution.radar_attribution(
            oracle_view, "a", oracle_view.data["room_snapshot"], human=False
        )
        probes = (_episode + 0.25, _episode + 6.25)
        answers = [oracle["truths"][desk](timestamp) for timestamp in probes]
        self.assertEqual([full["truths"][desk](timestamp) for timestamp in probes], answers)
        attribution.release([first, first])  # Linked groups share the same view.
        self.assertFalse(first._native_found_job)
        self.runtime.data["room_label_revision"][desk] += 1
        changed = attribution.prepared_view(self.runtime, "a")
        with patch.object(event_attribution, "_found", wraps=event_attribution._found) as reads:
            self.assertEqual(seen_by._member_episodes(changed, "a", desk), expected)
        self.assertGreater(
            reads.call_count, 0, "changed label revision reused stale sample answers"
        )

    async def test_clear_discards_maintained_buffers_and_old_view_cannot_reinstall(self):
        from tuner_under_test.rooms import attribution, seen_by

        desk, _episode, _members, _room = self.shared_radar_desk()
        view = attribution.prepared_view(self.runtime, "a")
        seen_by._member_episodes(view, "a", desk)
        self.assertTrue(self.runtime._days.days("a"))
        self.runtime.clear_samples("a")
        attribution.release([view])  # The old job finishes after the clear.
        self.assertFalse(self.runtime._days.days("a"), "old job cannot restore cleared history")
        self.assertFalse(getattr(self.runtime, "_native_found_job", {}))

    async def test_old_completed_view_does_not_replace_newly_appended_history(self):
        from tuner_under_test.history import recording, spans
        from tuner_under_test.rooms import attribution, seen_by

        desk, _episode, _members, _room = self.shared_radar_desk()
        view = attribution.prepared_view(self.runtime, "a")
        seen_by._member_episodes(view, "a", desk)
        self.runtime._record_history_sample("a", {"g0_move": 80}, self.now + 6)
        self.read_time = self.now + 12
        latest = list(spans.events(self.runtime, "a"))
        mark = recording.history_mark(self.runtime, "a")
        attribution.release([view])  # The old job finishes after new history arrived.
        self.assertEqual(recording.history_mark(self.runtime, "a"), mark)
        self.assertEqual(list(spans.events(self.runtime, "a")), latest)
        self.assertEqual((latest[-1].start, latest[-1].end), (self.now + 6, self.now + 12))
        self.assertEqual(latest[-1].values[0], 80)

    async def test_a_group_outside_the_snapshot_never_reaches_the_shared_attribution(self):
        """The roles fallback (a group the radar's snapshot does not hold) computes alone: a
        made-up one-group result must not enter the job's memo or the attribution cache."""
        from tuner_under_test.rooms import attribution, roles

        desk, _episode, members, _room = self.shared_radar_desk()
        view = attribution.prepared_view(self.runtime, "a")
        members = {"a": {**members["a"], "view": view}}
        elsewhere = {"group_id": "zone:elsewhere", "groups": {}, "room_labels": {}}
        roles.attributed_yes(members, elsewhere)
        roles.target(members, elsewhere, "a")(0)
        for human in (True, False):
            real = attribution.radar_attribution(view, "a", view.data["room_snapshot"], human=human)
            self.assertIn(desk, real["found"], "a made-up snapshot's result entered the memo")

    async def learn_office_set(self, *patchers):
        """Learn the office area (radars a and b, human labels) with the ``patchers`` active;
        returns what they give. The job must complete."""
        from tuner_under_test.rooms import learning

        self.office()
        t, area = self.now - 3600, []
        for state, value in (("present", 40), ("not_present", 2)):
            area.append({"start": t, "end": t + 360, "state": state, "source": "manual"})
            for step in range(60):
                self.guess(t + step * 6, state)
                self.runtime._record_history_sample("a", {"g0_move": value}, t + step * 6)
                self.runtime._record_history_sample("b", {"g0_move": 2}, t + step * 6 + 3)
            t += 360
        self.runtime.data["room_labels"] = {"area:office": area}
        self.runtime.data["room_label_revision"] = {"area:office": 1}
        with ExitStack() as stack:
            stack.enter_context(patch.object(learning.recovery, "prepare_learning", AsyncMock()))
            stack.enter_context(patch.object(learning.time, "time", return_value=t + 60))
            entered = [stack.enter_context(patcher) for patcher in patchers]
            report = learning.start_set(self.runtime, "area:office", "automatic")
            await self.runtime._room_learning_jobs[report["group_id"]]
        job = self.runtime.data["room_learning"]["area:office"]["job"]
        self.assertEqual(job["status"], "completed", job.get("error"))
        return entered

    async def test_learning_a_set_decodes_each_radars_rows_once(self):
        """Set learning reuses each frozen radar's physical folds throughout search."""
        with self.native_builds() as builds:
            await self.learn_office_set()
        self.assert_native_builds(builds, "ab")
        self.assertFalse(getattr(self.runtime, "_native_found_job", {}))

    async def test_a_device_job_attributes_its_radar_once(self):
        """Regression: a device-level job on an unprepared view made two attribution passes
        (human truth, then full truth), each decoding every row; prepared, it makes one."""
        from tuner_under_test.calibration import service

        await self.learn_office_set()
        self.runtime.hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *a: fn(*a))
        entities, current = {"g0_move": "number.a_g0_move"}, {"g0_move": 20}
        with (
            patch.object(
                service.recovery, "prepare_learning", AsyncMock(return_value=(entities, current))
            ),
            patch.object(service, "read_timing", return_value={}),
            self.native_builds() as builds,
        ):
            await service._learn_once(self.runtime, "a")
        self.assert_native_builds(builds, "a")
        self.assertFalse(getattr(self.runtime, "_native_found_job", {}))

    def assert_released(self, views):
        """A finished job's views hold no attribution results and no member guesses."""
        self.assertTrue(views)
        for view in views:
            self.assertFalse(getattr(view, "_native_found_job", {}))
            self.assertFalse(getattr(view, "_attribution_guesses", {}))

    async def test_a_set_job_frees_its_views_in_the_executor(self):
        """Regression: dropping the views of a finished job freed millions of small objects
        on the event loop (0.2 s at 30 days; seconds on a Pi)."""
        from tuner_under_test.rooms import attribution

        views = []

        def prepared(runtime, key, shared=None, real=attribution.prepared_view, **kwargs):
            views.append(real(runtime, key, shared, **kwargs))
            return views[-1]

        await self.learn_office_set(
            patch.object(attribution, "prepared_view", side_effect=prepared)
        )
        self.assert_released(views)

    async def test_a_seen_by_build_frees_its_views_in_the_executor(self):
        from tuner_under_test.rooms import attribution

        views = []

        def prepared(runtime, key, shared=None, real=attribution.prepared_view, **kwargs):
            views.append(real(runtime, key, shared, **kwargs))
            return views[-1]

        self.office()
        self.record(10, quiet_b=True)
        with patch.object(attribution, "prepared_view", side_effect=prepared):
            await self.poll()
        self.assert_released(views)

    async def test_a_device_job_frees_its_view_before_scoring(self):
        from tuner_under_test.calibration import service
        from tuner_under_test.rooms import attribution

        await self.learn_office_set()
        self.runtime.hass.async_add_executor_job = AsyncMock(side_effect=lambda fn, *a: fn(*a))
        entities, current = {"g0_move": "number.a_g0_move"}, {"g0_move": 20}
        views = []

        def prepared(runtime, key, shared=None, real=attribution.prepared_view, **kwargs):
            views.append(real(runtime, key, shared, **kwargs))
            return views[-1]

        with (
            patch.object(
                service.recovery, "prepare_learning", AsyncMock(return_value=(entities, current))
            ),
            patch.object(service, "read_timing", return_value={}),
            patch.object(service.attribution, "prepared_view", side_effect=prepared),
        ):
            await service._learn_once(self.runtime, "a")
        self.assert_released(views)

    async def test_overlapping_member_episodes_share_one_row(self):
        self.office()
        starts = self.record(10, quiet_b=True)  # 60 quiet samples each: above the floor
        await self.poll()
        area = (await self.poll())["room_attribution"]["area:office"]
        self.assertEqual([row["start"] for row in area], starts)
        self.assertEqual({row["devices"]["a"]["state"] for row in area}, {"yes"})
        self.assertEqual({row["devices"]["b"]["state"] for row in area}, {"no"})

    async def test_weak_inferred_results_show_unknown_as_the_learner_drops_them(self):
        self.office()
        self.record(4)  # 24 quiet samples: baseline confidence 0.24, below the floor.
        await self.poll()
        rows = (await self.poll())["room_attribution"][self.zone]
        self.assertEqual(
            rows[-1]["devices"]["a"], {"state": "unknown", "confidence": 0.24, "inferred": True}
        )

    def later_run(self, start, values, zone_labels=(), guess=None):
        for step, value in enumerate(values):
            if guess:
                self.guess(start + step * 6, guess)
            self.runtime._record_history_sample("a", {"g0_move": value}, start + step * 6)
        self.runtime.data["devices"]["a"].setdefault("auto", {})["last_classification"] = {}
        self.runtime.data["room_labels"][self.zone] += list(zone_labels)
        self.runtime.data["room_label_revision"][self.zone] += 1
        self.read_time = max(self.read_time, start + len(values) * 6)

    async def test_rows_split_where_the_learner_switches_attribution(self):
        self.office()
        starts = self.record(10)  # 60 quiet samples: confidence 0.6
        b = starts[-1] + 144
        # Human Present for the first half; the recorded estimate keeps it occupied after.
        human = {"start": b, "end": b + 36, "state": "present", "source": "manual"}
        self.later_run(b, [60] * 12, [human], guess="present")
        await self.poll()
        rows = (await self.poll())["room_attribution"][self.zone]
        self.assertEqual(
            [(row["start"] - b, row["end"] - b, row["estimated"]) for row in rows[-2:]],
            # Open physical coverage reaches the pinned view time.
            [(0, 36, False), (36, 72, True)],
        )

    async def test_one_radars_neighbouring_episodes_keep_their_own_rows(self):
        self.office()
        starts = self.record(10)
        b = starts[-1] + 144
        labels = [
            {"start": b, "end": b + 36, "state": "present", "source": "manual"},
            {"start": b + 36, "end": b + 42, "state": "not_present", "source": "manual"},
            {"start": b + 42, "end": b + 78, "state": "present", "source": "manual"},
        ]
        self.later_run(b, [60] * 6 + [2] * 7, labels)
        await self.poll()
        rows = (await self.poll())["room_attribution"][self.zone]
        self.assertEqual(
            [(row["start"] - b, row["devices"]["a"]["state"]) for row in rows[-2:]],
            [(0, "yes"), (42, "no")],
        )

    def test_a_cached_estimate_still_expires_on_time(self):
        from tuner_under_test.rooms import room_labels

        self.office()
        zone = groups.configure(self.runtime, None, "Desk", "office", ["a"])["id"]
        bucket = room_labels.ESTIMATE_CACHE_SECONDS
        poll = (int(self.now // bucket) + 2) * bucket + 0.1
        latest = poll - room_labels.FEEDBACK_WINDOW_SECONDS + 0.5
        for step in range(10):
            self.guess(latest - 54 + step * 6, "present")
            self.runtime._record_history_sample("a", {"g0_move": 60}, latest - 54 + step * 6)
        from tuner_under_test.history import ingest

        ingest.end(self.runtime, "a", latest)
        with patch.object(room_labels.time, "time", return_value=poll):
            self.assertIsNotNone(
                jobs.snapshot_with_inventory(self.runtime)[0]["groups"][zone]["estimate"]
            )
        # Same cache bucket, but now more than the feedback window after the last sample.
        with patch.object(room_labels.time, "time", return_value=poll + bucket - 0.2):
            self.assertIsNone(
                jobs.snapshot_with_inventory(self.runtime)[0]["groups"][zone]["estimate"]
            )

    def test_removed_groups_leave_the_estimate_cache(self):
        self.office()
        zone = groups.configure(self.runtime, None, "Desk", "office", ["a"])["id"]
        jobs.snapshot_with_inventory(self.runtime)[0]
        self.assertIn(zone, self.runtime._room_estimates)
        groups.remove(self.runtime, zone)
        jobs.snapshot_with_inventory(self.runtime)[0]
        self.assertNotIn(zone, self.runtime._room_estimates)

    async def test_samples_not_yet_in_a_block_are_attributed(self):
        self.office()
        starts = self.record(2, flush=False)
        self.assertEqual(self.runtime.data["devices"]["a"].get("history", []), [])
        await self.poll()
        episodes = (await self.poll())["room_attribution"][self.zone]
        self.assertEqual([item["start"] for item in episodes], starts)

    def test_groups_without_recordings_need_no_job(self):
        self.office()
        snapshot = self.runtime.snapshot()
        self.assertEqual(snapshot["room_attribution"], {"area:office": []})
        self.assertEqual(self.runtime._room_attribution_jobs, {})

    async def test_failed_build_is_not_retried_every_poll(self):
        from tuner_under_test.rooms import seen_by

        self.office()
        self.record(2)
        with patch.object(seen_by, "build", side_effect=ValueError("broken")) as spy:
            await self.poll()
            snapshot = await self.poll()
        self.assertEqual(spy.call_count, 2)
        self.assertEqual(snapshot["room_attribution"][self.zone], [])

    async def test_removed_group_forgets_its_episodes(self):
        self.office()
        self.record(2)
        await self.poll()
        self.assertIn(self.zone, self.runtime._room_attribution)
        groups.remove(self.runtime, self.zone)
        snapshot = await self.poll()
        self.assertNotIn(self.zone, snapshot["room_attribution"])
        self.assertNotIn(self.zone, self.runtime._room_attribution)

    async def test_change_during_build_is_rebuilt_and_never_runs_twice(self):
        self.office()
        self.record(2)
        started, finish = asyncio.Event(), asyncio.Event()
        calls = []

        async def executor(fn, *args):
            calls.append(args[2])
            started.set()
            await finish.wait()
            return fn(*args)

        self.hass.async_add_executor_job = executor
        self.runtime.snapshot()
        await asyncio.wait_for(started.wait(), 5)
        await asyncio.sleep(0)
        self.runtime.data["room_label_revision"][self.zone] = 2
        self.runtime.snapshot()
        self.assertEqual(len(calls), 1, "builds queue one at a time, never twice per group")
        self.assertEqual(len(self.runtime._room_attribution_jobs), 2)
        finish.set()
        await asyncio.gather(*self.runtime._room_attribution_jobs.values())
        await self.poll()
        self.assertEqual(calls.count(self.zone), 2, "the result of stale inputs is rebuilt")

    async def test_stop_cancels_pending_builds(self):
        from tuner_under_test.runtime import schedule

        self.office()
        self.record(2)
        started = asyncio.Event()

        async def executor(fn, *args):
            started.set()
            await asyncio.Event().wait()

        self.hass.async_add_executor_job = executor
        self.runtime.snapshot()
        await asyncio.wait_for(started.wait(), 5)
        tasks = list(self.runtime._room_attribution_jobs.values())
        await schedule.stop(self.runtime)
        self.assertTrue(tasks and all(task.cancelled() for task in tasks))
