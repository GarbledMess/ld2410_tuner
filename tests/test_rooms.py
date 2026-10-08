"""Room replay and grouping boundaries use synthetic, independently labelled zones."""

import asyncio
import types
import unittest
from copy import deepcopy
from unittest.mock import patch

import test_tuner as harness
from tuner_under_test.rooms import assessment, evidence, groups, intervals, jobs, replay


def member(device_id, values, labels, timing=None, offset=0):
    records = [(offset + i * 6, [value] + [255] * 17 + [0, 0]) for i, value in enumerate(values)]
    device = {"history_labels": labels, "training_state": "unknown"}
    view = types.SimpleNamespace(
        data={"devices": {device_id: device}}, _iter_history_samples=lambda *a, **kw: iter(records)
    )
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
        self.assertEqual(empty["active_seconds"], 27)
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
        self.assertEqual(report["shared_recording_seconds"], 594)
        self.assertIsNone(report["room"]["score"])
        self.assertEqual(report["excluded"]["no_shared_recording_seconds"], 606)

    def test_incomplete_sample_breaks_coverage(self):
        values = [0] * 200
        values[100] = 255
        report = assessment.calculate({"a": member("a", values, labels())}, 0, 1200)
        self.assertEqual(report["shared_recording_seconds"], 1182)

    def test_per_radar_timing_precedes_union(self):
        rows_a = [(i, {"g0_move": 40 if i < 5 else 0}, "unknown") for i in range(30)]
        rows_b = [(i, {"g0_move": 40 if 8 <= i < 12 else 0}, "unknown") for i in range(30)]
        timing_a = {"timeout": 10, "on_delay": 0, "off_delay": 0}
        timing_b = {"timeout": 1, "on_delay": 0, "off_delay": 0}
        a = replay.radar(rows_a, {"g0_move": 20}, timing_a)
        b = replay.radar(rows_b, {"g0_move": 20}, timing_b)
        combined = replay.lookups(replay.combined({"a": a, "b": b}))
        self.assertTrue(combined["lower"].at(13))  # a's hold still covers the handoff
        self.assertFalse(replay.lookups(b)["lower"].at(13, False))
        self.assertFalse(combined["lower"].at(20, False))

    def test_label_priority_and_unknown_coverage(self):
        automatic = intervals.Timeline([(0, 100, ("present", "automatic", 0.8))])
        a = {"manual": lambda t: "not_present", "automatic": automatic}
        b = {"manual": lambda t: None, "automatic": intervals.Timeline([])}
        self.assertEqual(evidence.device_label(a, 10), ("not_present", "human", 1))
        self.assertIsNone(evidence.room_label({"a": a, "b": b}, 10))
        a["manual"] = lambda t: "unknown"
        self.assertIsNone(evidence.device_label(a, 10))
        a["manual"] = lambda t: "present"
        self.assertEqual(evidence.room_label({"a": a, "b": b}, 10), ("present", "human", 1))

    def test_automatic_scores_identify_estimated_evidence(self):
        a = member("a", [40] * 100 + [0] * 100, [])
        records = [
            (i * 6, [40 if i < 100 else 0] + [255] * 17 + [1 if i < 100 else 2, 80])
            for i in range(200)
        ]
        a["view"]._iter_history_samples = lambda *args, **kw: iter(records)
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
        self.assertEqual(report["shared_recording_seconds"], 1191)
        self.assertLessEqual(report["room"]["presence_recall"], 1)


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
                    jobs.snapshot(self.runtime)["groups"]["area:office"]["assessment"]["state"],
                    "ready",
                )
        with patch.object(assessment, "SCORER_VERSION", 2):
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
