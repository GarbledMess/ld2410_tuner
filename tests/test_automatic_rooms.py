"""Rooms train from automatic evidence alone: no human labels, still assessed, learned, applied."""

import unittest
from copy import deepcopy
from unittest.mock import patch

import test_joint_learning as joint
import test_rooms as harness
from test_rooms import set_records
from tuner_under_test.engine.hybrid import adapters
from tuner_under_test.engine.hybrid.exact import Evaluator
from tuner_under_test.engine.hybrid.fixed import source
from tuner_under_test.rooms import application, jobs, room_labels, truth

CURRENT = 60  # Above the occupied signal (40), so the radar misses every occupied sample.
SPLIT = 600
SENSOR_GROUP = "area:office"


def sensor_labels():
    """Room sensor labels (automatic, confidence 0.9) for the occupied then empty halves."""
    return [
        {"start": 0, "end": SPLIT, "state": "present", "source": "sensor", "confidence": 0.9},
        {
            "start": SPLIT,
            "end": 1200,
            "state": "not_present",
            "source": "sensor",
            "confidence": 0.9,
        },
    ]


def records(guess):
    """Radar history: 40 while occupied, 0 after. ``guess`` adds the radar's own recorded
    automatic guess (member evidence) at full confidence: a present guess counts only from a fresh
    reference (``constants.TRUST_PRESENT``)."""
    rows = []
    for i in range(200):
        occupied = i < 100
        label = (1 if occupied else 2) if guess else 0
        rows.append((i * 6, [40 if occupied else 0] + [255] * 17 + [label, 100 if guess else 0]))
    return rows


class AutomaticRoomTests(unittest.IsolatedAsyncioTestCase):
    setUp = harness.GroupTests.setUp
    asyncTearDown = harness.GroupTests.asyncTearDown
    office = harness.GroupTests.office
    radars = joint.SetRuntimeTests.radars
    zones = joint.SetRuntimeTests.zones
    linked = joint.SetRuntimeTests.linked
    learning_patches = joint.SetRuntimeTests.learning_patches
    learn = joint.SetRuntimeTests.learn
    writer = joint.SetRuntimeTests.writer

    def unlabelled_history(self, guess):
        """Every radar's history has no human labels at all."""
        data = records(guess)

        def view(key):
            found = harness.member(key, [], [])["view"]
            set_records(found, key, data)
            return found

        self.runtime._history_view = view

    def set_sensor(self, group_id):
        self.runtime.data.setdefault("room_labels", {})[group_id] = sensor_labels()

    async def assess(self, group_id):
        with patch.object(jobs.time, "time", return_value=1200):
            jobs.start(self.runtime, group_id, 1)
            await self.runtime._room_jobs[group_id]
        return jobs.summary(self.runtime, group_id)

    def assert_lowered(self, thresholds, *radars):
        for radar in radars:
            assert thresholds[radar]["g0_move"] < CURRENT
            assert 0 < thresholds[radar]["g0_move"] <= 40

    # (a) assessed ----------------------------------------------------------------

    async def test_sensor_only_zone_is_assessed_as_estimated(self):
        self.radars(current=CURRENT)
        self.unlabelled_history(guess=False)
        self.set_sensor(SENSOR_GROUP)
        report = await self.assess(SENSOR_GROUP)
        assert report["state"] == "ready"
        assert report["room"]["score"] is not None
        assert report["room"]["basis"] == "estimated"
        outcomes = report["room"]["outcomes"]
        assert outcomes["human"]["present"]["seconds"] == 0
        assert outcomes["automatic"]["present"]["seconds"] > 500

    async def test_estimate_only_zone_is_assessed_as_estimated(self):
        self.radars(current=CURRENT)
        self.unlabelled_history(guess=True)
        report = await self.assess(SENSOR_GROUP)
        assert report["room"]["score"] is not None
        assert report["room"]["basis"] == "estimated"

    async def test_zone_without_any_evidence_is_not_scorable(self):
        self.radars(current=CURRENT)
        self.unlabelled_history(guess=False)
        report = await self.assess(SENSOR_GROUP)
        assert report["room"]["score"] is None

    # (b) one-group learning, (d) manual Apply, (e) both evidence kinds -----------

    async def learn_and_apply(self, guess):
        self.radars(current=CURRENT)
        self.unlabelled_history(guess=guess)
        if not guess:
            self.set_sensor(SENSOR_GROUP)
        learned = await self.learn(SENSOR_GROUP)
        job = self.runtime.data["room_learning"][SENSOR_GROUP]["job"]
        assert job["status"] == "completed", job.get("error")
        assert learned["after"]["room"]["basis"] == "estimated"
        self.assert_lowered(learned["thresholds"], "a", "b")
        assert learned["cost_after"] < learned["cost_before"]
        assert (
            learned["after"]["room"]["presence_recall"]
            > learned["before"]["room"]["presence_recall"]
        )
        # Automatic Apply stays a safety gate for estimated evidence; manual Apply works.
        writes, (prepare, write) = self.writer()
        with prepare, write:
            result = await application.apply(self.runtime, SENSOR_GROUP, learned["id"], "user")
        assert not result["skipped"]
        written = {(device, value) for device, _key, value in writes}
        assert {device for device, _ in written} == {"a", "b"}
        for device, _key, value in writes:
            assert 0 < value <= 40, device
        assert (
            self.runtime.data["room_learning"][SENSOR_GROUP]["application"]["status"] == "applied"
        )

    async def test_sensor_only_zone_learns_and_applies_manually(self):
        await self.learn_and_apply(guess=False)

    async def test_estimate_only_zone_learns_and_applies_manually(self):
        await self.learn_and_apply(guess=True)

    async def test_manual_learn_of_estimated_only_zone_is_not_auto_applied_by_default(self):
        self.radars(current=CURRENT)
        self.unlabelled_history(guess=False)
        self.set_sensor(SENSOR_GROUP)
        await self.learn(SENSOR_GROUP)
        report = self.runtime.data["room_learning"][SENSOR_GROUP]["job"]["automatic_apply"]
        assert report["status"] == "skipped"
        assert report["reason"] == (
            "Automatic Apply must be enabled for this source on every zone member"
        )
        assert self.hass.services.async_call.await_count == 0

    # (c) linked set: one auto-only zone, one human-labelled zone -----------------

    async def test_linked_set_learns_auto_only_zone_beside_human_zone(self):
        # This case tests positive automatic preferences beside a human zone.
        # The zero ceiling is covered separately and removes those tie preferences.
        self.runtime.data["learning_schedule"] = {"automatic_weight": 0.5}
        set_id = self.linked(current=CURRENT)
        self.unlabelled_history(guess=False)
        desk, sofa = self.ids["Desk"], self.ids["Sofa"]
        self.set_sensor(desk)
        room_labels.label_room_range(self.runtime, sofa, 0, SPLIT, "present")
        room_labels.label_room_range(self.runtime, sofa, SPLIT, 1200, "not_present")
        learned = await self.learn(set_id)
        job = self.runtime.data["room_learning"][set_id]["job"]
        assert job["status"] == "completed", job.get("error")
        zones = learned["groups"]
        assert "skipped" not in zones[desk] and "skipped" not in zones[sofa]
        assert zones[desk]["after"]["room"]["basis"] == "estimated"
        assert zones[sofa]["after"]["room"]["basis"] == "human"
        # Radar a serves only the auto-only zone, c only the human one, b both.
        self.assert_lowered(learned["thresholds"], "a", "b", "c")
        assert zones[desk]["cost_after"] < zones[desk]["cost_before"]
        writes, (prepare, write) = self.writer()
        with prepare, write:
            result = await application.apply(self.runtime, set_id, learned["id"], "user")
        assert not result["skipped"]
        assert {device for device, _key, _value in writes} == {"a", "b", "c"}

    async def test_zero_ceiling_scores_shared_union_and_apply_skips_unchanged_member(self):
        self.runtime.data["learning_schedule"] = {"automatic_weight": 0}
        set_id = self.linked(current=CURRENT)
        self.unlabelled_history(guess=False)
        desk, sofa = self.ids["Desk"], self.ids["Sofa"]
        self.set_sensor(desk)
        room_labels.label_room_range(self.runtime, sofa, 0, SPLIT, "present")
        room_labels.label_room_range(self.runtime, sofa, SPLIT, 1200, "not_present")
        learned = await self.learn(set_id)
        self.assert_lowered(learned["thresholds"], "a", "b")
        # Hybrid chooses interior gaps and then lower total configured thresholds,
        # including a redundant member when its detections do not worsen the zone.
        self.assert_lowered(learned["thresholds"], "c")
        forced = deepcopy(learned["thresholds"])
        forced["c"] = {"g0_move": CURRENT}
        for gid, keys in ((desk, ("a", "b")), (sofa, ("b", "c"))):
            members = {
                key: {
                    "view": self.runtime._history_view(key),
                    "thresholds": learned["thresholds"][key],
                    "timing": learned["timing"][key],
                }
                for key in keys
            }
            redundant = joint.fitting.measure(
                members, forced, 0, 1200, truth.view(self.runtime, gid)
            )["room"]
            actual = learned["groups"][gid]["after"]["room"]
            # The last loud reading holds until the next one, exactly at the state boundary:
            # every present second is detected, and raising redundant c changes nothing.
            self.assertAlmostEqual(actual["presence_recall"], 1.0)
            self.assertAlmostEqual(redundant["presence_recall"], 1.0)
            assert actual["false_positive_percent"] == redundant["false_positive_percent"] == 0
            assert actual["target_met"] == redundant["target_met"] is True
        # Exercise Apply's no-op guard with the equally accurate shared-radar pattern.
        learned["thresholds"] = forced
        writes, (prepare, write) = self.writer()
        with prepare, write:
            result = await application.apply(self.runtime, set_id, learned["id"], "user")
        assert not result["skipped"]
        # Joint Apply preserves redundant c without issuing unsupported/no-op writes.
        assert {device for device, _key, _value in writes} == {"a", "b"}
        assert "c" not in result["applied"]
        assert "last_applied" not in self.runtime.data["devices"]["c"]

    async def test_linked_set_learns_when_both_zones_are_automatic_only(self):
        set_id = self.linked(current=CURRENT)
        self.unlabelled_history(guess=True)
        learned = await self.learn(set_id)
        assert self.runtime.data["room_learning"][set_id]["job"]["status"] == "completed"
        for zone in learned["groups"].values():
            assert zone["after"]["room"]["basis"] == "estimated"
        self.assert_lowered(learned["thresholds"], "a", "b", "c")


def office_room(group_id, *labels):
    office = {"name": "Office", "area_id": "office", "kind": "area", "device_ids": ["a"]}
    return {
        "group_id": group_id,
        "groups": {group_id: office},
        "room_labels": {group_id: list(labels)},
    }


def radar(threshold):
    item = harness.member("a", [40] * 100 + [0] * 100, [])
    item["thresholds"]["g0_move"] = threshold
    return {"a": item}


def costs(members, room, value, weight=0):
    problem = adapters.room_problem({"zone": members}, 0, 1200, {"zone": room}, weight)
    pattern = {"a": {0: value}}
    with Evaluator(problem, ((0, 1200),)) as evaluator:
        total = evaluator.scores([pattern])[0]
        human = evaluator.scores([pattern], zones=(source(problem.zones[0], "human"),))[0]
        automatic = evaluator.scores([pattern], zones=(source(problem.zones[0], "automatic"),))[0]
    return total, human, automatic


class AutomaticCostTests(unittest.TestCase):
    """Source precedence applies to the shared full time and episode cost."""

    def test_automatic_only_room_ranks_primarily_on_automatic_cost(self):
        room = office_room("area:office", *sensor_labels())
        missed, human, automatic = costs(radar(CURRENT), room, CURRENT)
        found, _, _ = costs(radar(CURRENT), room, 30)
        assert missed["cost"] == automatic["cost"] > 100 > found["cost"]
        assert human["occupied_seconds"] == human["empty_seconds"] == 0

    def test_automatic_weight_does_not_double_count_automatic_only_cost(self):
        room = office_room("area:office", *sensor_labels())
        plain, _, automatic = costs(radar(CURRENT), room, CURRENT)
        blended, _, _ = costs(radar(CURRENT), room, CURRENT, weight=1)
        assert blended["cost"] == plain["cost"] == automatic["cost"]

    def half_labelled(self):
        return office_room(
            "area:office",
            {"start": SPLIT + 100, "end": 1200, "state": "not_present", "source": "manual"},
            sensor_labels()[0],
        )

    def test_half_labelled_room_ranks_missed_automatic_present_after_equal_human_cost(self):
        room = self.half_labelled()
        missed, human_miss, _ = costs(radar(CURRENT), room, CURRENT)
        found, human_found, _ = costs(radar(CURRENT), room, 30)
        assert human_miss["false_active_seconds"] == human_found["false_active_seconds"] == 0
        assert human_miss["cost"] is human_found["cost"] is None
        assert found["cost"] < missed["cost"]

    def test_automatic_sourced_classes_order_ties_not_the_whole_automatic_cost(self):
        values = [40] * 85 + [20] * 15 + [15] * 34 + [0] * 66
        room = office_room(
            "area:office",
            {"start": 900, "end": 1200, "state": "not_present", "source": "manual"},
            sensor_labels()[0],
            {
                "start": 600,
                "end": 800,
                "state": "not_present",
                "source": "sensor",
                "confidence": 0.9,
            },
        )
        members = {"a": harness.member("a", values, [])}
        a, ah, aa = costs(members, room, 30)
        b, bh, ba = costs(members, room, 10)
        assert ah["false_active_seconds"] == bh["false_active_seconds"] == 0
        assert ah["cost"] is bh["cost"] is None
        assert aa["cost"] < ba["cost"]
        assert b["cost"] < a["cost"]

    def test_automatic_present_never_overrules_human_empty(self):
        members = {"a": harness.member("a", [40] * 200, [])}
        detects, _, _ = costs(members, self.half_labelled(), 20)
        quiet, _, _ = costs(members, self.half_labelled(), 60)
        assert quiet["cost"] < detects["cost"]

    def test_half_labelled_room_reports_cost_per_class_source(self):
        for value in (CURRENT, 30):
            total, human, automatic = costs(radar(CURRENT), self.half_labelled(), value)
            stats = total["sufficient_statistics"]
            assert "present/automatic" in stats
            assert "not_present/human" in stats
            assert total["cost"] == 100 * (total["time_term"] + total["episode_term"])
            assert human["occupied_seconds"] == automatic["empty_seconds"] == 0

    def test_half_labelled_room_blends_automatic_class_but_not_twice(self):
        plain, _, _ = costs(radar(CURRENT), self.half_labelled(), CURRENT)
        blended, _, _ = costs(radar(CURRENT), self.half_labelled(), CURRENT, weight=1)
        assert blended["cost"] == plain["cost"] > 0

    def test_room_with_human_truth_keeps_human_primary_cost(self):
        manual = [
            {"start": 0, "end": SPLIT, "state": "present", "source": "manual"},
            {"start": SPLIT, "end": 1200, "state": "not_present", "source": "manual"},
        ]
        total, human, automatic = costs(
            radar(CURRENT), office_room("area:office", *manual, *sensor_labels()), CURRENT
        )
        assert total["cost"] == human["cost"] > 0
        assert human["occupied_recall"] == 0
        assert automatic["occupied_seconds"] == 0

    def test_linked_set_counts_automatic_only_zone_in_primary_cost(self):
        room = office_room("area:office", *sensor_labels())
        problem = adapters.room_problem({"zone": radar(CURRENT)}, 0, 1200, {"zone": room}, 0)
        with Evaluator(problem, ((0, 1200),)) as evaluator:
            assert evaluator.scores([{"a": {0: CURRENT}}])[0]["cost"] > 0
