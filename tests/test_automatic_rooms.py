"""Rooms train from automatic evidence alone: no human labels, still assessed, learned, applied."""

import unittest
from copy import deepcopy
from unittest.mock import patch

import test_joint_learning as joint
import test_rooms as harness
from test_rooms import set_records
from tuner_under_test.calibration.scoring import error_cost
from tuner_under_test.rooms import application, jobs, room_labels, truth
from tuner_under_test.rooms.objective import LinkedObjective, Objective

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

    async def test_zero_ceiling_preserves_redundant_member_when_shared_radar_covers_union(self):
        self.runtime.data["learning_schedule"] = {"automatic_weight": 0}
        set_id = self.linked(current=CURRENT)
        self.unlabelled_history(guess=False)
        desk, sofa = self.ids["Desk"], self.ids["Sofa"]
        self.set_sensor(desk)
        room_labels.label_room_range(self.runtime, sofa, 0, SPLIT, "present")
        room_labels.label_room_range(self.runtime, sofa, SPLIT, 1200, "not_present")
        learned = await self.learn(set_id)
        self.assert_lowered(learned["thresholds"], "a", "b")
        assert learned["thresholds"]["c"]["g0_move"] == CURRENT
        forced = deepcopy(learned["thresholds"])
        forced["c"] = dict(forced["b"])
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
            # every present second is detected, and lowering c changes nothing.
            self.assertAlmostEqual(actual["presence_recall"], 1.0)
            self.assertAlmostEqual(redundant["presence_recall"], 1.0)
            assert actual["false_positive_percent"] == redundant["false_positive_percent"] == 0
            assert actual["target_met"] == redundant["target_met"] is True
        assert learned["groups"][sofa]["after"]["devices"]["c"]["presence_recall"] == 0
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


class AutomaticCostTests(unittest.TestCase):
    """With no human truth in the window, the ranked primary cost is the automatic one."""

    def test_automatic_only_room_ranks_primarily_on_automatic_cost(self):
        room = office_room("area:office", *sensor_labels())
        objective = Objective(radar(CURRENT), 0, 1200, room)
        missed = objective.rank({"a": {"g0_move": CURRENT}})
        found = objective.rank({"a": {"g0_move": 30}})
        assert missed[0] > 100 > found[0]
        assert missed[1] > 100 > found[1]

    def test_automatic_weight_does_not_double_count_automatic_only_cost(self):
        room = office_room("area:office", *sensor_labels())
        plain = Objective(radar(CURRENT), 0, 1200, room).rank({"a": {"g0_move": CURRENT}})
        blended = Objective(radar(CURRENT), 0, 1200, room, automatic_weight=1.0).rank(
            {"a": {"g0_move": CURRENT}}
        )
        assert blended == plain
        assert blended[0] == blended[1]  # Primary is the automatic cost alone.

    def half_labelled(self, signal=None, **options):
        """Human Empty only, automatic (sensor) Present only."""
        room = office_room(
            "area:office",
            {"start": SPLIT + 100, "end": 1200, "state": "not_present", "source": "manual"},
            sensor_labels()[0],
        )
        return Objective(signal or radar(CURRENT), 0, 1200, room, **options)

    def test_half_labelled_room_ranks_missed_automatic_present_after_equal_human_cost(self):
        objective = self.half_labelled()
        # Both are quiet through the human Empty half, so human cost ties at 0.
        missed = objective.rank({"a": {"g0_move": CURRENT}})
        found = objective.rank({"a": {"g0_move": 30}})
        assert missed[0] == found[0] == 0
        assert found < missed

    def test_automatic_sourced_classes_order_ties_not_the_whole_automatic_cost(self):
        # Human Empty (900-1200), automatic Empty (600-800), automatic-only Present (0-594).
        # A (30) misses the last 15 percent of Present; B (10) detects all of it but is
        # active through the automatic Empty. Human cost ties at 0. The full automatic cost
        # (A 75 < B 100) would pick A; the automatic-sourced class (Present) picks B.
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
        item = harness.member("a", values, [])
        objective = Objective({"a": item}, 0, 1200, room)
        a, b = ({"a": {"g0_move": value}} for value in (30, 10))
        ranked_a, ranked_b = objective.rank(a), objective.rank(b)
        assert ranked_a[0] == ranked_b[0] == 0  # Human cost ties.
        rates_a, rates_b = objective.rates(a)["automatic"], objective.rates(b)["automatic"]
        assert error_cost(*rates_a) < error_cost(*rates_b)  # Whole automatic cost prefers A.
        assert ranked_b < ranked_a

    def test_automatic_present_never_overrules_human_empty(self):
        item = harness.member("a", [40] * 200, [])
        objective = self.half_labelled({"a": item})
        detects = objective.rank({"a": {"g0_move": 20}})  # Hits the automatic Present.
        quiet = objective.rank({"a": {"g0_move": 60}})  # Misses it, clean on human Empty.
        assert quiet[0] < detects[0]
        assert quiet < detects

    def test_half_labelled_room_reports_cost_per_class_source(self):
        objective = self.half_labelled()
        for value in (CURRENT, 30):
            thresholds = {"a": {"g0_move": value}}
            rates = objective.rates(thresholds)
            assert objective.costs(thresholds)[0] == error_cost(
                rates["automatic"][0], rates["human"][1]
            )

    def test_half_labelled_room_blends_automatic_class_but_not_twice(self):
        plain, blended = self.half_labelled(), self.half_labelled(automatic_weight=1.0)
        assert plain.shares == (0, 0)
        assert blended.shares[0] > 0  # Present has no human mass: its share still blends.
        thresholds = {"a": {"g0_move": CURRENT}}
        assert blended.rank(thresholds)[0] > plain.rank(thresholds)[0] == 0
        assert blended.rank(thresholds)[1] == plain.rank(thresholds)[1] == 0

    def test_room_with_human_truth_keeps_human_primary_cost(self):
        manual = [
            {"start": 0, "end": SPLIT, "state": "present", "source": "manual"},
            {"start": SPLIT, "end": 1200, "state": "not_present", "source": "manual"},
        ]
        room = office_room("area:office", *manual, *sensor_labels())
        objective = Objective(radar(CURRENT), 0, 1200, room)
        human = objective.rates({"a": {"g0_move": CURRENT}})["human"]
        assert objective.rank({"a": {"g0_move": CURRENT}})[0] > 0
        assert human[0] > 0.9

    def test_linked_set_counts_automatic_only_zone_in_primary_cost(self):
        room = office_room("area:office", *sensor_labels())
        linked = LinkedObjective({"area:office": radar(CURRENT)}, 0, 1200, {"area:office": room})
        assert linked.rank({"a": {"g0_move": CURRENT}})[0] > 0


if __name__ == "__main__":
    unittest.main()
