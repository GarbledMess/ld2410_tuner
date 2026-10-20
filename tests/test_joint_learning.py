"""Joint fits preserve complementary and redundant detection, without inventing timing."""

import asyncio
import types
import unittest
from copy import deepcopy
from unittest.mock import AsyncMock, patch

import pytest
import test_rooms as harness
from test_rooms import labels, member, set_records
from tuner_under_test.calibration.separation import gate_preference
from tuner_under_test.rooms import application, fitting, groups, learning, objective
from tuner_under_test.rooms.objective import LinkedObjective, Objective


def example(a, b, current=20):
    data = {"a": member("a", a, labels()), "b": member("b", b, labels())}
    for item in data.values():
        item["thresholds"]["g0_move"] = current
    return data


def test_strong_radar_does_not_force_weak_radar_to_detect():
    data = example([70] * 100 + [10] * 100, [3] * 200)
    result = fitting.fit(data, 0, 1200)
    assert result["thresholds"]["b"]["g0_move"] == 20
    assert 10 < result["thresholds"]["a"]["g0_move"] < 70
    assert result["cost_after"] <= result["cost_before"]


def test_both_strong_radars_retained_instead_of_picking_one_winner():
    data = example([70] * 100 + [10] * 100, [50] * 100 + [5] * 100, current=100)
    result = fitting.fit(data, 0, 1200)
    assert 10 < result["thresholds"]["a"]["g0_move"] < 70
    assert 5 < result["thresholds"]["b"]["g0_move"] < 50
    for item in result["after"]["devices"].values():
        assert item["outcomes"]["human"]["present"]["active_seconds"] > 590
    assert result["after"]["room"]["outcomes"]["human"]["present"]["active_seconds"] < 601


def test_complementary_handoffs_and_noisy_member():
    data = example([70] * 50 + [3] * 150, [3] * 50 + [60] * 50 + [30] * 100)
    result = fitting.fit(data, 0, 1200)
    assert result["thresholds"]["b"]["g0_move"] >= 30
    assert result["thresholds"]["b"]["g0_move"] < 60
    assert result["after"]["room"]["presence_recall"] > 0.99
    assert result["after"]["room"]["false_positive_percent"] == 0
    assert result["cost_after"] < result["cost_before"]


def test_search_cost_matches_report_with_different_timing_and_offsets():
    data = example([70] * 100 + [10] * 100, [50] * 100 + [5] * 100)
    data["a"]["timing"] = {"timeout": 1, "on_delay": 0.5, "off_delay": 1}
    data["b"]["timing"] = {"timeout": 8, "on_delay": 2, "off_delay": 3}
    objective = Objective(data, 0, 1200)
    for value in (0, 10, 30, 55, 75, 100):
        thresholds = {"a": {"g0_move": value}, "b": {"g0_move": 55 - value if value < 55 else 20}}
        report = fitting.measure(data, thresholds, 0, 1200)["room"]
        expected = 500 * (1 - report["presence_recall"]) + report["false_positive_percent"]
        assert abs(objective.costs(thresholds)[0] - expected) < 1e-8


def test_automatic_presence_cannot_overrule_human_empty_labels():
    item = member("a", [70] * 200, [{"start": 600, "end": 1200, "state": "not_present"}])
    records = [(i * 6, [70] + [255] * 17 + [1, 100]) for i in range(200)]
    set_records(item["view"], "a", records)
    result = fitting.fit({"a": item}, 0, 1200)
    assert result["thresholds"]["a"]["g0_move"] >= 70
    assert result["after"]["room"]["outcomes"]["human"]["not_present"]["active_seconds"] == 0


def test_quiet_member_samples_do_not_shift_its_preference():
    quiet = [i % 10 for i in range(100)]
    empty_only = [{"start": 600, "end": 1200, "state": "not_present"}]
    data = {
        "a": member("a", [70] * 100 + [10] * 100, labels()),
        "b": member("b", [10] * 100 + quiet, empty_only),
    }
    objective = Objective(data, 0, 1200)
    noise = [(600 + i * 6, {"g0_move": value}, "not_present", 1.0) for i, value in enumerate(quiet)]
    nothing = {"present": [], "not_present": []}
    expected = gate_preference("g0_move", {"present": [], "not_present": noise}, nothing, 20)
    seen = [(i * 6, {"g0_move": 10}, "present", 1.0) for i in range(100)]
    counted = gate_preference("g0_move", {"present": seen, "not_present": noise}, nothing, 20)
    assert counted["preferred_threshold"] != expected["preferred_threshold"]
    assert objective.preferences["b"]["g0_move"] == expected
    # The radar that rose keeps its Present evidence.
    assert objective.preferences["a"]["g0_move"]["human_supported"]


def office_room(*labels):
    office = {"name": "Office", "area_id": "office", "kind": "area", "device_ids": ["a", "b"]}
    return {
        "group_id": "area:office",
        "groups": {"area:office": office},
        "room_labels": {"area:office": list(labels)},
    }


def test_unlabelled_member_that_rises_keeps_present_evidence_and_quiet_one_does_not():
    quiet = [i % 10 for i in range(100)]
    data = {"a": member("a", [70] * 100 + quiet, []), "b": member("b", [10] * 100 + quiet, [])}
    room = office_room(
        {"start": 0, "end": 600, "state": "present", "source": "manual"},
        {"start": 600, "end": 1200, "state": "not_present", "source": "manual"},
    )
    preferences = Objective(data, 0, 1200, room).preferences
    # Neither radar is labelled, so rule 2 (its own empty baseline) decides attribution: an
    # inference, so a's Present rows are automatic evidence (confidence 100/100), not human.
    noise = [(600 + i * 6, {"g0_move": value}, "not_present", 1.0) for i, value in enumerate(quiet)]
    nothing = {"present": [], "not_present": []}
    seen = {"present": [(i * 6, {"g0_move": 70}, "present", 1.0) for i in range(100)]}
    inferred = gate_preference(
        "g0_move", {"present": [], "not_present": noise}, {**nothing, **seen}, 20
    )
    assert preferences["a"]["g0_move"] == inferred
    assert preferences["a"]["g0_move"]["presence_reference"] == 70
    assert not preferences["a"]["g0_move"]["human_supported"]
    expected = gate_preference("g0_move", {"present": [], "not_present": noise}, nothing, 20)
    assert preferences["b"]["g0_move"] == expected
    assert preferences["b"]["g0_move"]["presence_reference"] is None


def test_rule_two_below_the_automatic_floor_adds_no_preference_rows():
    # 40 quiet samples give a rule-2 confidence of 0.4 < MIN_AUTO_CONFIDENCE: no Present row.
    quiet = [i % 10 for i in range(40)]
    data = {"a": member("a", [70] * 100 + quiet, []), "b": member("b", [10] * 140, [])}
    room = office_room(
        {"start": 0, "end": 600, "state": "present", "source": "manual"},
        {"start": 600, "end": 840, "state": "not_present", "source": "manual"},
    )
    preference = Objective(data, 0, 840, room).preferences["a"]["g0_move"]
    noise = [(600 + i * 6, {"g0_move": value}, "not_present", 1.0) for i, value in enumerate(quiet)]
    nothing = {"present": [], "not_present": []}
    assert preference == gate_preference("g0_move", {**nothing, "not_present": noise}, nothing, 20)
    assert preference["presence_reference"] is None


def test_below_floor_rule_two_keeps_a_more_confident_room_estimate():
    # As above, but both radars' guesses make the room estimate Present at 0.9975 > the 0.4
    # guess (a present estimate needs two members saying so): automatic Present evidence for a.
    quiet = [i % 10 for i in range(40)]
    data = {"a": member("a", [70] * 100 + quiet, []), "b": member("b", [10] * 140, [])}
    records = [(i * 6, [70] + [255] * 17 + [1, 100]) for i in range(100)]
    records += [(600 + i * 6, [v] + [255] * 17 + [2, 100]) for i, v in enumerate(quiet)]
    set_records(data["a"]["view"], "a", records)
    records = [(i * 6, [10] + [255] * 17 + [1, 100]) for i in range(100)]
    set_records(data["b"]["view"], "b", records)
    room = office_room(
        {"start": 0, "end": 600, "state": "present", "source": "manual"},
        {"start": 600, "end": 840, "state": "not_present", "source": "manual"},
    )
    preference = Objective(data, 0, 840, room).preferences["a"]["g0_move"]
    assert preference["presence_reference"] == 70
    assert not preference["human_supported"]


def test_own_empty_label_while_room_occupied_feeds_the_baseline():
    # The room is occupied throughout (a is labelled Present). b's only quiet evidence is its
    # own Empty label in the first recording block; its second block is unlabelled and rises.
    quiet = [i % 10 for i in range(100)]
    data = {
        "a": member("a", [70] * 267, [{"start": 0, "end": 1600, "state": "present"}]),
        "b": member("b", quiet, [{"start": 0, "end": 600, "state": "not_present"}]),
    }
    records = [(i * 6, [value] + [255] * 17 + [0, 0]) for i, value in enumerate(quiet)]
    records += [(1000 + i * 6, [60] + [255] * 17 + [0, 0]) for i in range(100)]
    set_records(data["b"]["view"], "b", records)
    preference = Objective(data, 0, 1600).preferences["b"]["g0_move"]
    # Without that baseline the second block would be "unknown" and contribute nothing.
    assert preference["presence_reference"] == 60


def joint_examples():
    """The four ``example`` inputs of the joint-fit tests above, plus a room-labelled one."""
    timed = example([70] * 100 + [10] * 100, [50] * 100 + [5] * 100)
    timed["a"]["timing"] = {"timeout": 1, "on_delay": 0.5, "off_delay": 1}
    timed["b"]["timing"] = {"timeout": 8, "on_delay": 2, "off_delay": 3}
    quiet = [i % 10 for i in range(100)]
    labelled = {"a": member("a", [70] * 100 + quiet, []), "b": member("b", [10] * 100 + quiet, [])}
    room = office_room(
        {"start": 0, "end": 600, "state": "present", "source": "manual"},
        {"start": 600, "end": 1200, "state": "not_present", "source": "manual"},
    )
    return [
        (example([70] * 100 + [10] * 100, [3] * 200), None),
        (example([70] * 100 + [10] * 100, [50] * 100 + [5] * 100, current=100), None),
        (example([70] * 50 + [3] * 150, [3] * 50 + [60] * 50 + [30] * 100), None),
        (timed, None),
        (labelled, room),
    ]


@pytest.mark.parametrize("index", range(5))
def test_one_group_set_matches_fit(index):
    data, room = joint_examples()[index]
    expected = fitting.fit(data, 0, 1200, room)
    data, room = joint_examples()[index]
    result = fitting.fit_set({"area:office": data}, 0, 1200, {"area:office": room})
    group = result.pop("groups")
    assert result == expected
    assert list(result["thresholds"]) == list(expected["thresholds"])
    assert group == {
        "area:office": {
            key: expected[key] for key in ("before", "after", "cost_before", "cost_after")
        }
    }


END = 1680  # 280 samples, six seconds apart


def zone(gid, companion, split):
    """A zone of ``companion`` and the shared radar ``s``, Present until ``split``."""
    quiet = [i % 10 for i in range(100)]
    shared = [70] * 60 + [40] * 60 + [25] * 60 + quiet
    members = {companion: member(companion, [3] * 280, []), "s": member("s", shared, [])}
    definition = {"name": gid, "area_id": "home", "kind": "zone", "device_ids": list(members)}
    room = {
        "group_id": gid,
        "groups": {gid: definition},
        "room_labels": {
            gid: [
                {"start": 0, "end": split, "state": "present", "source": "manual"},
                {"start": split, "end": END, "state": "not_present", "source": "manual"},
            ]
        },
    }
    return members, room


def linked(*specs):
    built = {gid: zone(gid, companion, split) for gid, companion, split in specs}
    groups = {gid: item[0] for gid, item in built.items()}
    return groups, {gid: item[1] for gid, item in built.items()}


DESK, SOFA, BED = ("zone:desk", "x", 360), ("zone:sofa", "y", 720), ("zone:bed", "z", 1080)


def own_preferred(groups, rooms, gid):
    return Objective(groups[gid], 0, END, rooms[gid]).preferred["s"]["g0_move"]


def test_shared_radar_preference_is_the_lower_median_of_group_preferences():
    groups, rooms = linked(DESK, SOFA, BED)
    own = {gid: own_preferred(groups, rooms, gid) for gid in groups}
    assert len(set(own.values())) == 3  # each zone attributes s differently
    three = LinkedObjective(groups, 0, END, rooms)
    assert three.preferred["s"]["g0_move"] == sorted(own.values())[1]
    # Groups are visited in sorted id order (bed, desk, sofa), whatever the input order.
    assert list(three.groups) == ["zone:bed", "zone:desk", "zone:sofa"]
    assert list(three.current) == list(three.radars) == ["z", "s", "x", "y"]
    pair, pair_rooms = linked(DESK, SOFA)
    two = LinkedObjective(pair, 0, END, pair_rooms)
    # Even count: the lower of the two middle values, never their mean.
    assert two.preferred["s"]["g0_move"] == min(own["zone:desk"], own["zone:sofa"])
    alone = Objective(pair["zone:desk"], 0, END, pair_rooms["zone:desk"])
    assert two.preferred["x"] == alone.preferred["x"]
    assert set(two.radars) == set(two.current) == {"x", "s", "y"}


def test_shared_radar_evidence_is_read_once_per_linked_objective():
    """Linked groups read and replay a shared radar's bounded evidence once. Its attribution
    is the learner's, over full history; a job computes that once per radar
    (``test_rooms``: ``test_a_job_attributes_each_radar_once``)."""
    groups, rooms = linked(DESK, SOFA, BED)
    with patch.object(objective.evidence, "read", wraps=objective.evidence.read) as spy:
        LinkedObjective(groups, 0, END, rooms)
    assert [call.args[1] for call in spy.call_args_list].count("s") == 1


def test_linked_rank_sums_group_ranks_and_rounds_away_summation_noise():
    groups, rooms = linked(DESK, SOFA, BED)
    objective = LinkedObjective(groups, 0, END, rooms)
    # Thresholds whose three group costs (9 places each) do not sum exactly in floats.
    thresholds = {key: {"g0_move": 7} for key in ("x", "y", "z", "s")}
    parts = [item.rank(part) for _, item, part in objective.parts(thresholds)]
    sums = [sum(values) for values in zip(*parts, strict=True)]
    assert sums[0] != round(sums[0], 9)  # three 9-place costs whose float sum is not
    # Keys: blended, human (P2b tie-break), automatic-sourced, automatic cost, then margins.
    costs = tuple(round(value, 9) for value in sums[:4])
    assert objective.rank(thresholds) == (*costs, *sums[4:])
    # A shared radar's change counts once per group containing it: x, y, z once, s three times.
    assert objective.rank(thresholds)[5] == 6


def test_linked_cost_is_sum_of_group_costs():
    groups, rooms = linked(DESK, SOFA)
    objective = LinkedObjective(groups, 0, END, rooms)
    alone = {gid: Objective(groups[gid], 0, END, rooms[gid]) for gid in groups}
    for shared in (0, 11, 27, 42, 100):
        thresholds = {"x": {"g0_move": 5}, "y": {"g0_move": 50}, "s": {"g0_move": shared}}
        parts = [
            item.costs({key: thresholds[key] for key in item.radars}) for item in alone.values()
        ]
        expected = tuple(sum(values) for values in zip(*parts, strict=True))
        assert objective.costs(thresholds) == pytest.approx(expected, abs=1e-12)
        assert len(objective.costs(thresholds)) == 2
    result = fitting.fit_set(groups, 0, END, rooms)
    for key in ("cost_before", "cost_after"):
        total = sum(item[key] for item in result["groups"].values())
        assert result[key] == pytest.approx(total, abs=1e-12)
    assert result["cost_after"] <= result["cost_before"]


def test_shared_radar_gets_single_value():
    groups, rooms = linked(DESK, SOFA, BED)
    result = fitting.fit_set(groups, 0, END, rooms)
    assert list(result["thresholds"]) == ["z", "s", "x", "y"]  # bed, desk, sofa: sorted ids
    assert list(result["groups"]) == ["zone:bed", "zone:desk", "zone:sofa"]
    for gid, members in groups.items():
        report = result["groups"][gid]
        expected = fitting.measure(members, result["thresholds"], 0, END, rooms[gid])
        assert report["after"] == expected
        alone = Objective(members, 0, END, rooms[gid])
        assert report["cost_after"] == alone.costs(result["thresholds"])[0]
        assert report["cost_before"] == alone.costs(alone.current)[0]
    assert isinstance(result["thresholds"]["s"]["g0_move"], int)


def test_shared_radar_value_minimises_the_summed_cost_not_one_zone():
    groups, rooms = linked(DESK, SOFA)
    result = fitting.fit_set(groups, 0, END, rooms)
    objective = LinkedObjective(groups, 0, END, rooms)
    # Fitted alone, the zones disagree about s; linked, s takes one value that is no worse
    # for the pair than either zone's own choice, and desk alone would have chosen otherwise.
    alone = {gid: fitting.fit(groups[gid], 0, END, rooms[gid]) for gid in groups}
    choices = {item["thresholds"]["s"]["g0_move"] for item in alone.values()}
    assert len(choices) == 2
    shared = result["thresholds"]["s"]["g0_move"]
    assert shared != alone["zone:desk"]["thresholds"]["s"]["g0_move"]
    for choice in choices:
        candidate = {**result["thresholds"], "s": {"g0_move": choice}}
        assert result["cost_after"] <= objective.costs(candidate)[0]
    assert result["cost_after"] < result["cost_before"]


def test_linked_fit_ignores_group_order_and_reports_no_union_room():
    groups, rooms = linked(SOFA, DESK)
    forward = fitting.fit_set(groups, 0, END, rooms)
    backward = fitting.fit_set(dict(reversed(groups.items())), 0, END, rooms)
    assert forward == backward
    # No room truth spans several zones: the top level carries no room aggregate, only each
    # radar's measure in every zone that scored it; per-zone results live in "groups".
    for stage in ("before", "after"):
        assert forward[stage] == {
            "room": None,
            "devices": {
                radar: {
                    gid: forward["groups"][gid][stage]["devices"][radar]
                    for gid in ("zone:desk", "zone:sofa")
                    if radar in groups[gid]
                }
                for radar in ("x", "s", "y")
            },
        }
    assert list(forward["after"]["devices"]["s"]) == ["zone:desk", "zone:sofa"]
    met = all(item["after"]["room"]["target_met"] for item in forward["groups"].values())
    assert forward["status"] == ("uncertain" if met else "unsafe")


def without_labels(groups, rooms, gid, companion):
    """Make ``gid`` unscorable: no room labels and an unlabelled, quiet companion."""
    rooms[gid]["room_labels"] = {}
    groups[gid] = {companion: member(companion, [3] * 280, []), "s": groups[gid]["s"]}
    current = {key: dict(item["thresholds"]) for key, item in groups[gid].items()}
    report = fitting.measure(groups[gid], current, 0, END, rooms[gid])
    assert report["room"]["score"] is None
    return report["room"]["reason"]


def test_unscorable_zone_is_skipped_and_the_set_fits_on_the_rest():
    groups, rooms = linked(DESK, SOFA)
    reason = without_labels(groups, rooms, "zone:sofa", "y")
    result = fitting.fit_set(groups, 0, END, rooms)
    desk = fitting.fit(groups["zone:desk"], 0, END, rooms["zone:desk"])
    assert result["groups"] == {
        "zone:desk": {key: desk[key] for key in ("before", "after", "cost_before", "cost_after")},
        "zone:sofa": {"skipped": reason},
    }
    # The shared radar is fitted for desk alone; y serves only the skipped zone and is not
    # searched, so it keeps its current threshold.
    assert result["thresholds"] == {**desk["thresholds"], "y": {"g0_move": 20}}
    assert list(result["thresholds"]) == ["x", "s", "y"]
    assert (result["cost_before"], result["cost_after"]) == (
        desk["cost_before"],
        desk["cost_after"],
    )
    assert result["status"] == desk["status"]
    assert result["after"]["room"] is None
    assert result["after"]["devices"] == {
        radar: {"zone:desk": desk["after"]["devices"][radar]} for radar in ("x", "s")
    }


def test_linked_fit_raises_only_when_no_zone_is_scorable():
    with pytest.raises(ValueError, match="No zones"):
        fitting.fit_set({}, 0, END)
    groups, rooms = linked(DESK, SOFA)
    first = without_labels(groups, rooms, "zone:desk", "x")
    without_labels(groups, rooms, "zone:sofa", "y")
    with pytest.raises(ValueError) as error:
        fitting.fit_set(groups, 0, END, rooms)
    assert str(error.value) == first
    # One unscorable zone alone raises exactly what fit raises.
    with pytest.raises(ValueError) as expected:
        fitting.fit(groups["zone:desk"], 0, END, rooms["zone:desk"])
    with pytest.raises(ValueError) as error:
        fitting.fit_set({"zone:desk": groups["zone:desk"]}, 0, END, rooms)
    assert str(error.value) == str(expected.value) == first


class JointRuntimeTests(unittest.IsolatedAsyncioTestCase):
    setUp = harness.GroupTests.setUp
    asyncTearDown = harness.GroupTests.asyncTearDown
    office = harness.GroupTests.office
    # Reuse the synthetic HA boundary without inheriting its test methods.
    __test__ = True

    async def admitted_members(self, chosen):
        from tuner_under_test.rooms import jobs

        members, _, rooms = await learning._prepare(self.runtime, chosen)
        self.assertTrue(
            all("view" not in item for found in members.values() for item in found.values())
        )
        previous = self.worker_transport.respond
        self.worker_transport.respond = AsyncMock(return_value={})
        try:
            await jobs.on_views(
                self.runtime, members, learning._fit_set, members, 0, 1200, rooms, 0
            )
        finally:
            self.worker_transport.respond = previous
        return members

    async def test_prepared_attribution_reads_the_last_week_by_default(self):
        self.office()
        chosen = groups.learning_set(self.runtime, "area:office")[2]
        now = 1_800_000_123
        with (
            patch.object(learning.recovery, "prepare_learning", AsyncMock()),
            patch.object(learning.time, "time", return_value=now),
        ):
            members = await self.admitted_members(chosen)
        expected = (now - 7 * 86400) // 3600 * 3600
        for found in members.values():
            for item in found.values():
                self.assertEqual(item["view"]._attribution_since, expected)

    async def test_exact_attribution_reads_all_retained_history(self):
        self.office()
        chosen = groups.learning_set(self.runtime, "area:office")[2]
        with (
            patch.object(learning.recovery, "prepare_learning", AsyncMock()),
            patch.object(learning, "ATTRIBUTION_DAYS", None),
        ):
            members = await self.admitted_members(chosen)
        for found in members.values():
            for item in found.values():
                self.assertEqual(item["view"]._attribution_since, 0)

    async def test_attribution_windows_have_separate_cache_keys(self):
        self.office()
        del self.runtime._history_view
        zone = groups.configure(self.runtime, None, "Desk", "office", ["a", "b"])
        chosen = groups.learning_set(self.runtime, zone["id"])[2]
        keys = []
        for days in (None, 7, 3):
            with (
                patch.object(learning.recovery, "prepare_learning", AsyncMock()),
                patch.object(learning.time, "time", return_value=1_800_000_123),
                patch.object(learning, "ATTRIBUTION_DAYS", days),
            ):
                members = await self.admitted_members(chosen)
            view = next(iter(next(iter(members.values())).values()))["view"]
            # Each window's view attributes only its own window (its job-local memo).
            keys.append(view._attribution_since)
        self.assertEqual(len(set(keys)), 3)

    async def test_learning_window_is_forwarded_to_the_fit(self):
        self.office()
        with (
            patch.object(learning.recovery, "prepare_learning", AsyncMock()),
            patch.object(learning.time, "time", return_value=1200),
            patch.object(learning, "LEARNING_DAYS", 3),
            patch.object(learning.fitting, "fit_set", wraps=learning.fitting.fit_set) as fit,
        ):
            await learning.start_device(self.runtime, "a", "user", "area:office")
        self.assertEqual(fit.call_args.args[1:3], (1200 - 3 * 86400, 1200))

    async def test_joint_job_deduplicates_device_requests_and_saves_both(self):
        self.office()
        prepare = AsyncMock()
        with (
            patch.object(learning.recovery, "prepare_learning", prepare),
            patch.object(learning.time, "time", return_value=1200),
        ):
            first = learning.start_device(self.runtime, "a", "user", "area:office")
            second = learning.start_device(self.runtime, "b", "automatic", "area:office")
            a, b = await asyncio.gather(first, second)
        assert a["user"]["id"] == b["automatic"]["id"]
        assert set(self.runtime.data["devices"]["b"]["learning_results"]) == {"user", "automatic"}
        assert prepare.await_count == 2
        assert self.device["learning_job"]["status"] == "completed"
        assert self.hass.services.async_call.await_count == 0

    def test_zone_precedence_and_ambiguous_membership(self):
        self.office()
        desk = groups.configure(self.runtime, None, "Desk", "office", ["a"])
        assert groups.for_device(self.runtime, "a") is None
        assert groups.for_device(self.runtime, "b") is None
        with self.assertRaisesRegex(
            ValueError,
            r"^This area is split into zones\. Learn its zones from their headers or linked band\.$",
        ):
            groups.learning_set(self.runtime, "area:office")
        groups.configure(self.runtime, desk["id"], "Desk", "office", ["a", "b"])
        assert groups.for_device(self.runtime, "b") == desk["id"]
        overlap = groups.configure(self.runtime, None, "Overlap", None, ["b", "a"])
        # Overlapping zones now form one linked learning set instead of an error.
        linked = "+".join(sorted((desk["id"], overlap["id"])))
        assert groups.for_device(self.runtime, "b") == linked

    async def _learn(self):
        self.office()
        with (
            patch.object(learning.recovery, "prepare_learning", AsyncMock()),
            patch.object(learning.time, "time", return_value=1200),
        ):
            await learning.start_device(self.runtime, "a", "user", "area:office")
        return self.runtime.data["room_learning"]["area:office"]["slots"]["user"]

    async def test_joint_apply_writes_both_and_blocks_changed_companion(self):
        learned = await self._learn()
        # Exercise actual legal changes; unchanged settings require no device writes.
        for key in ("a", "b"):
            learned["thresholds"][key]["g0_move"] = 30
        values = {
            key: dict(signature["thresholds"])
            for key, signature in learned["signature"].items()
            if key in ("a", "b")
        }
        self.runtime._threshold_configuration = lambda key: (
            {"g0_move": f"number.{key}"},
            dict(values[key]),
        )
        writes = []

        async def write(runtime, device, key, value, button):
            writes.append((device, key, value))
            values[device][key] = value

        with (
            patch.object(application.device_io, "prepare_device", AsyncMock(return_value=None)),
            patch.object(application.device_io, "write_threshold", write),
        ):
            result = await application.apply(self.runtime, "area:office", learned["id"], "user")
        assert not result["skipped"]
        assert {entry[0] for entry in writes} == {"a", "b"}
        assert (
            self.runtime.data["devices"]["b"]["learning_results"]["current"]["id"] == learned["id"]
        )
        values["b"]["g0_move"] = 99
        with self.assertRaisesRegex(ValueError, "zone member"):
            application.guard(self.runtime, learned, {})

    async def test_one_member_disallowing_auto_apply_blocks_entire_group(self):
        learned = await self._learn()
        self.runtime.data["learning_schedule"] = {"enabled": True, "auto_apply": True}
        self.runtime.data["devices"]["b"]["auto_apply_override"] = "off"
        report = {}
        await application.automatic(self.runtime, learned, report, "automatic")
        assert report["status"] == "skipped"
        assert "every zone member" in report["reason"]
        assert self.hass.services.async_call.await_count == 0

    async def test_partial_write_never_rotates_group_current(self):
        learned = await self._learn()
        for key in ("a", "b"):
            learned["thresholds"][key]["g0_move"] = 30
        with (
            patch.object(application.device_io, "prepare_device", AsyncMock(return_value=None)),
            patch.object(
                application.device_io,
                "write_threshold",
                AsyncMock(side_effect=ValueError("offline")),
            ),
        ):
            outcome = await application.apply(self.runtime, "area:office", learned["id"], "user")
        assert outcome["skipped"]
        for device in self.runtime.data["devices"].values():
            assert "current" not in device["learning_results"]

    async def test_existing_device_learn_routes_to_joint_job(self):
        from tuner_under_test.calibration import jobs as device_jobs

        self.office()
        with (
            patch.object(learning.recovery, "prepare_learning", AsyncMock()),
            patch.object(learning.time, "time", return_value=1200),
        ):
            report = device_jobs.start_learning(self.runtime, "a")
            assert report["group_id"] == "area:office"
            await self.runtime._learning_jobs["a"]
        assert (
            self.runtime.data["devices"]["b"]["last_learning"]["joint"]["group_id"] == "area:office"
        )

    async def test_joint_result_cannot_be_applied_as_one_radar(self):
        learned = await self._learn()
        with self.assertRaisesRegex(ValueError, "complete joint"):
            await self.runtime.apply("a", "user", learned["id"], {"g0_move": 20})
        assert self.hass.services.async_call.await_count == 0

    async def test_overnight_pass_visits_a_joint_group_once(self):
        from tuner_under_test.runtime import schedule

        self.office()
        self.runtime.data["learning_schedule"] = {"enabled": True, "auto_apply": False}
        self.runtime.async_save = AsyncMock()
        prepare = AsyncMock()
        with (
            patch.object(learning.recovery, "prepare_learning", prepare),
            patch.object(learning.time, "time", return_value=1200),
        ):
            await schedule._run(self.runtime, "2026-10-07")
        assert prepare.await_count == 2
        a, b = (item["nightly_learning"] for item in self.runtime.data["devices"].values())
        assert a["result_id"] == b["result_id"]
        assert a["automatic_apply"]["status"] == "skipped"

    def number_entities(self, device_id, threshold):
        # Home Assistant reports number entities as float strings, e.g. "20.0".
        states = {
            f"radar_{device_id}_max_{kind}_distance_gate": "2.0" for kind in ("move", "still")
        }
        for gate in range(3):
            for kind in ("move", "still"):
                states[f"radar_{device_id}_g{gate}_{kind}_threshold"] = threshold
        for name, state in states.items():
            entity_id = f"number.{name}"
            self.registry.entities[entity_id] = types.SimpleNamespace(
                device_id=device_id, domain="number", entity_id=entity_id
            )
            self.states[entity_id] = types.SimpleNamespace(state=state)

    async def test_overnight_joint_learn_reads_float_threshold_states(self):
        from tuner_under_test.runtime import schedule

        self.office()
        del self.runtime._threshold_configuration
        for device_id in ("a", "b"):
            self.number_entities(device_id, "20.0")
        # Gates 0-2 recorded, matching the 2-gate distance limit.
        records = [(i * 6, [40 if i < 100 else 0] * 6 + [255] * 12 + [0, 0]) for i in range(200)]

        def history_view(key):
            view = member(key, [], labels())["view"]
            set_records(view, key, records)
            return view

        self.runtime._history_view = history_view
        self.runtime.data["learning_schedule"] = {"enabled": True, "auto_apply": False}
        self.runtime.async_save = AsyncMock()
        with (
            patch.object(learning.recovery, "prepare_learning", AsyncMock()),
            patch.object(learning.time, "time", return_value=1200),
        ):
            await schedule._run(self.runtime, "2026-10-08")
        for device in self.runtime.data["devices"].values():
            attempt = device["nightly_learning"]
            assert attempt["status"] != "error", attempt.get("error")
            assert attempt["result_id"]

    def float_state_office(self):
        """Office zone read through the real threshold reader from "20.0" number states."""
        self.office()
        del self.runtime._threshold_configuration
        for device_id in ("a", "b"):
            self.number_entities(device_id, "20.0")
        # Gates 0-2 recorded, matching the 2-gate distance limit.
        records = [(i * 6, [40 if i < 100 else 0] * 6 + [255] * 12 + [0, 0]) for i in range(200)]

        def history_view(key):
            view = member(key, [], labels())["view"]
            set_records(view, key, records)
            return view

        self.runtime._history_view = history_view

    async def test_user_started_room_learn_reads_float_threshold_states(self):
        self.float_state_office()
        with (
            patch.object(learning.recovery, "prepare_learning", AsyncMock()),
            patch.object(learning.time, "time", return_value=1200),
        ):
            report = self.runtime.learn_set("area:office")
            assert report["status"] == "running"
            await self.runtime._room_learning_jobs["area:office"]
        job = self.runtime.data["room_learning"]["area:office"]["job"]
        assert job["status"] == "completed", job.get("error")
        saved = self.runtime.data["room_learning"]["area:office"]["slots"]["user"]
        assert saved["id"] == job["results"]["user"]
        for device_id in ("a", "b"):
            thresholds = saved["signature"][device_id]["thresholds"]
            assert thresholds and all(type(value) is int for value in thresholds.values())
            assert self.runtime.data["devices"][device_id]["learning_results"]["user"]["id"]

    async def test_room_assessment_reads_float_threshold_states(self):
        from tuner_under_test.rooms import jobs

        self.float_state_office()
        with patch.object(jobs.time, "time", return_value=1200):
            jobs.start(self.runtime, "area:office", 1)
            await self.runtime._room_jobs["area:office"]
        saved = self.runtime.data["room_assessments"]["area:office"]
        assert "score" in saved["report"]["room"]
        assert jobs.summary(self.runtime, "area:office")["state"] == "ready"
        for device_id in ("a", "b"):
            thresholds = saved["signature"][device_id]["thresholds"]
            assert thresholds and all(type(value) is int for value in thresholds.values())


CHANGED = "Zone labels, settings or membership changed; learn again"
PAUSED = "Enable recording for every member, or remove paused radars from this learning group"


class SetRuntimeTests(unittest.IsolatedAsyncioTestCase):
    """Learning sets learn, apply and run overnight as one unit (spec 5)."""

    setUp = harness.GroupTests.setUp
    asyncTearDown = harness.GroupTests.asyncTearDown
    office = harness.GroupTests.office
    __test__ = True

    def radars(self, *keys, current=20):
        self.office()
        for key in keys:
            self.runtime.data["devices"].setdefault(key, deepcopy(self.device))
        self.values = {key: {"g0_move": current} for key in self.runtime.data["devices"]}
        self.removed = set()

        def configuration(key):
            if key in self.removed:
                raise ValueError("Enable all active gate threshold entities before learning")
            return {"g0_move": f"number.{key}"}, dict(self.values[key])

        self.runtime._threshold_configuration = configuration

    def zones(self, **members):
        ids = {
            name: groups.configure(self.runtime, None, name, "office", list(keys))["id"]
            for name, keys in members.items()
        }
        return ids, "+".join(sorted(ids.values()))

    def linked(self, current=20):
        """Desk (a, b) and Sofa (b, c) share radar b: one learning set."""
        self.radars("c", current=current)
        self.ids, set_id = self.zones(Desk="ab", Sofa="bc")
        return set_id

    def learning_patches(self, prepare=None):
        return (
            patch.object(learning.recovery, "prepare_learning", prepare or AsyncMock()),
            patch.object(learning.time, "time", return_value=1200),
        )

    async def learn(self, key, source="user"):
        first, second = self.learning_patches()
        with first, second:
            report = learning.start_set(self.runtime, key, source)
            await self.runtime._room_learning_jobs[report["group_id"]]
        return self.runtime.data["room_learning"][report["group_id"]]["slots"][source]

    def writer(self, fail_on=None):
        writes = []

        async def write(runtime, device, key, value, button):
            if (device, key) == fail_on:
                raise ValueError("offline")
            writes.append((device, key, value))
            self.values[device][key] = value

        return writes, (
            patch.object(application.device_io, "prepare_device", AsyncMock(return_value=None)),
            patch.object(application.device_io, "write_threshold", write),
        )

    async def test_learn_set_saves_all_members(self):
        set_id = self.linked()
        learned = await self.learn(set_id)
        job = self.runtime.data["room_learning"][set_id]["job"]
        assert job["status"] == "completed", job.get("error")
        assert job["group_id"] == set_id
        assert job["group_ids"] == sorted(self.ids.values())
        assert job["results"] == {"user": learned["id"]}
        assert set(learned["thresholds"]) == {"a", "b", "c"}
        assert set(learned["groups"]) == set(self.ids.values())
        assert all("cost_after" in item for item in learned["groups"].values())
        for key in "abc":
            device = self.runtime.data["devices"][key]
            saved = device["learning_results"]["user"]
            assert saved["id"] == learned["id"]
            assert saved["joint"]["group_id"] == set_id
            assert saved["joint"]["device_ids"] == ["a", "b", "c"]
            assert saved["joint"]["report"]["room"]["score"] is not None
            assert device["learning_job"]["status"] == "completed"

    async def test_device_learn_resolves_to_its_linked_set(self):
        from tuner_under_test.calibration import jobs as device_jobs

        set_id = self.linked()
        first, second = self.learning_patches()
        with first, second:
            report = device_jobs.start_learning(self.runtime, "c")
            assert report["group_id"] == set_id
            await self.runtime._learning_jobs["c"]
        saved = self.runtime.data["room_learning"][set_id]["slots"]["user"]
        assert self.runtime.data["devices"]["a"]["last_learning"]["id"] == saved["id"]

    async def test_apply_set_writes_union_lower_first(self):
        set_id = self.linked()
        learned = await self.learn(set_id)
        learned["thresholds"] = {"a": {"g0_move": 30}, "b": {"g0_move": 5}, "c": {"g0_move": 25}}
        writes, (prepare, write) = self.writer()
        with prepare, write:
            result = await self.runtime.apply_set(set_id, learned["id"], "user")
        assert not result["skipped"]
        assert writes == [("b", "g0_move", 5), ("c", "g0_move", 25), ("a", "g0_move", 30)]
        assert self.runtime.data["room_learning"][set_id]["application"]["status"] == "applied"
        for key in "abc":
            saved = self.runtime.data["devices"][key]["learning_results"]
            assert saved["current"]["id"] == learned["id"]
            assert saved["previous"]["configuration"] == {"g0_move": 20}

    async def test_partial_set_apply_never_rotates_any_member(self):
        set_id = self.linked()
        learned = await self.learn(set_id)
        learned["thresholds"] = {"a": {"g0_move": 30}, "b": {"g0_move": 5}, "c": {"g0_move": 25}}
        writes, (prepare, write) = self.writer(fail_on=("c", "g0_move"))
        with prepare, write:
            result = await self.runtime.apply_set(set_id, learned["id"], "user")
        assert writes == [("b", "g0_move", 5)]
        assert result["skipped"] == {"c.g0_move": "offline"}
        assert self.runtime.data["room_learning"][set_id]["application"]["status"] == "partial"
        for key in "abc":
            saved = self.runtime.data["devices"][key]["learning_results"]
            assert "current" not in saved
            assert "previous" not in saved

    async def automatic(self, learned):
        self.runtime.data["learning_schedule"] = {"enabled": True, "auto_apply": True}
        report = {}
        writes, (prepare, write) = self.writer()
        # Fresh comparisons read the last week of recordings, which end at t=1200.
        with prepare, write, patch.object(application, "time", return_value=1200):
            await application.automatic(self.runtime, learned, report, "automatic")
        return report, writes

    async def test_auto_apply_refuses_when_a_zone_worsens(self):
        set_id = self.linked()
        learned = await self.learn(set_id)
        zone = learned["groups"][self.ids["Sofa"]]
        zone["cost_after"] = zone["cost_before"] + 0.5
        report, writes = await self.automatic(learned)
        assert report["status"] == "skipped"
        assert report["reason"] == "A zone would get worse"
        assert writes == []
        assert self.runtime.data["room_learning"][set_id]["automatic_apply"]["status"] == "skipped"

    async def test_auto_apply_refuses_when_a_zone_is_unscored(self):
        set_id = self.linked()
        learned = await self.learn(set_id)
        learned["groups"][self.ids["Desk"]] = {"skipped": "No occupied and empty labels"}
        report, writes = await self.automatic(learned)
        assert report["status"] == "skipped"
        assert report["reason"].startswith("A zone has no usable labels")
        assert "No occupied and empty labels" in report["reason"]
        assert writes == []

    async def test_auto_apply_refuses_when_one_set_member_does_not_permit_it(self):
        set_id = self.linked()
        learned = await self.learn(set_id)
        self.runtime.data["devices"]["c"]["auto_apply_override"] = "off"
        report, writes = await self.automatic(learned)
        assert report["status"] == "skipped"
        assert "every zone member" in report["reason"]
        assert writes == []

    async def test_auto_apply_set_writes_every_member_when_no_zone_worsens(self):
        set_id = self.linked(current=50)
        learned = await self.learn(set_id, "automatic")
        assert all(item["cost_after"] < item["cost_before"] for item in learned["groups"].values())
        report, writes = await self.automatic(learned)
        assert report["status"] == "applied", report.get("reason")
        assert {device for device, _key, _value in writes} == {"a", "b", "c"}
        zones = report["assessment"]["zones"]
        assert set(zones) == set(self.ids.values())
        for item in zones.values():
            assert item["automatic"]["error_cost"] < item["live"]["error_cost"]
        totals = report["assessment"]["patterns"]
        assert totals["automatic"]["error_cost"] < totals["live"]["error_cost"]
        for key in "abc":
            current = self.runtime.data["devices"][key]["learning_results"]["current"]
            assert current["id"] == learned["id"]

    async def test_auto_apply_set_refuses_when_fresh_recordings_show_a_zone_worse(self):
        set_id = self.linked()
        learned = await self.learn(set_id)
        # Deafening b and c leaves Desk covered by a, but Sofa would miss its presence.
        learned["thresholds"].update(b={"g0_move": 100}, c={"g0_move": 100})
        report, writes = await self.automatic(learned)
        assert report["status"] == "skipped"
        assert report["reason"] == "A zone would get worse"
        zones = report["assessment"]["zones"]
        sofa, desk = zones[self.ids["Sofa"]], zones[self.ids["Desk"]]
        assert sofa["automatic"]["error_cost"] > sofa["live"]["error_cost"]
        assert desk["automatic"]["error_cost"] == desk["live"]["error_cost"]
        assert writes == []

    async def test_auto_apply_set_refuses_when_fresh_recordings_show_no_improvement(self):
        set_id = self.linked()
        learned = await self.learn(set_id)
        report, writes = await self.automatic(learned)
        assert report["status"] == "skipped"
        assert "does not reduce" in report["reason"]
        assert writes == []

    async def test_overnight_visits_each_set_once(self):
        from tuner_under_test.runtime import schedule

        set_id = self.linked()
        self.runtime.data["learning_schedule"] = {"enabled": True, "auto_apply": False}
        self.runtime.async_save = AsyncMock()
        prepare = AsyncMock()
        first, second = self.learning_patches(prepare)
        with first, second, patch.object(fitting, "fit_set", wraps=fitting.fit_set) as fit:
            await schedule._run(self.runtime, "2026-10-10")
        assert fit.call_count == 1
        # The shared radar b is prepared and learned once, not once per zone.
        assert sorted(call.args[1] for call in prepare.await_args_list) == ["a", "b", "c"]
        learned = self.runtime.data["room_learning"][set_id]["slots"]["automatic"]
        for key in "abc":
            attempt = self.runtime.data["devices"][key]["nightly_learning"]
            assert attempt["status"] == learned["status"], attempt.get("error")
            assert attempt["result_id"] == learned["id"]
            assert attempt["day"] == "2026-10-10"
            assert attempt["automatic_apply"]["status"] == "skipped"

    async def test_member_removed_mid_job_reports_changed_and_other_sets_continue(self):
        from tuner_under_test.runtime import schedule

        self.radars("c", "d")
        ids, _ = self.zones(Desk="ab")
        others, _ = self.zones(Sofa="cd")
        real = fitting.fit_set

        def remove_b(found, *args, **kwargs):
            if any("b" in members for members in found.values()):
                # Home Assistant removes radar b's entities while the fit is running.
                self.runtime.data["devices"]["b"]["entities"] = {}
                self.removed.add("b")
            return real(found, *args, **kwargs)

        self.runtime.data["learning_schedule"] = {"enabled": True, "auto_apply": False}
        self.runtime.async_save = AsyncMock()
        first, second = self.learning_patches()
        with first, second, patch.object(fitting, "fit_set", remove_b):
            await schedule._run(self.runtime, "2026-10-10")
        devices = self.runtime.data["devices"]
        for key in "ab":
            assert devices[key]["nightly_learning"]["status"] == "error"
            assert devices[key]["nightly_learning"]["error"] == CHANGED
        assert "slots" not in self.runtime.data["room_learning"][ids["Desk"]]
        learned = self.runtime.data["room_learning"][others["Sofa"]]["slots"]["automatic"]
        for key in "cd":
            assert devices[key]["nightly_learning"]["status"] == learned["status"]
            assert devices[key]["nightly_learning"]["result_id"] == learned["id"]

    def overnight_ready(self):
        self.runtime.data["learning_schedule"] = {"enabled": True, "auto_apply": False}
        self.runtime.async_save = AsyncMock()

    async def test_overnight_skips_a_set_with_no_learnable_member_and_runs_the_rest(self):
        from tuner_under_test.runtime import schedule

        self.radars("c", "d")
        ids, _ = self.zones(Desk="ab")
        others, _ = self.zones(Sofa="cd")
        # Desk keeps its definition, but neither radar can be learned tonight.
        self.runtime.data["devices"]["a"]["entities"] = {}
        self.runtime.data["devices"]["b"]["recording_enabled"] = False
        self.overnight_ready()
        first, second = self.learning_patches()
        with first, second:
            await schedule._run(self.runtime, "2026-10-10")
        devices = self.runtime.data["devices"]
        assert "nightly_learning" not in devices["a"]
        assert "nightly_learning" not in devices["b"]
        assert ids["Desk"] not in self.runtime.data["room_learning"]
        learned = self.runtime.data["room_learning"][others["Sofa"]]["slots"]["automatic"]
        for key in "cd":
            assert devices[key]["nightly_learning"]["result_id"] == learned["id"]

    async def test_radar_deleted_mid_run_is_not_resurrected_and_other_sets_continue(self):
        from tuner_under_test.runtime import schedule

        self.radars("c", "d")
        ids, _ = self.zones(Desk="ab")
        others, _ = self.zones(Sofa="cd")
        real = fitting.fit_set

        def delete_b(found, *args, **kwargs):
            if any("b" in members for members in found.values()):
                # b is deleted from Home Assistant and from stored devices during the fit.
                del self.runtime.data["devices"]["b"]
            return real(found, *args, **kwargs)

        self.overnight_ready()
        first, second = self.learning_patches()
        with first, second, patch.object(fitting, "fit_set", delete_b):
            await schedule._run(self.runtime, "2026-10-10")
        devices = self.runtime.data["devices"]
        assert "b" not in devices
        assert devices["a"]["nightly_learning"]["error"] == CHANGED
        learned = self.runtime.data["room_learning"][others["Sofa"]]["slots"]["automatic"]
        for key in "cd":
            assert devices[key]["nightly_learning"]["result_id"] == learned["id"]

    async def test_unreadable_groups_do_not_abort_the_overnight_pass(self):
        from tuner_under_test.runtime import schedule

        self.radars("c")
        self.overnight_ready()
        broken = patch.object(groups, "definitions", side_effect=ValueError("registry broken"))
        with broken, self.assertLogs(schedule._LOGGER, "WARNING") as logs:
            await schedule._run(self.runtime, "2026-10-10")
        assert any("registry broken" in line for line in logs.output)
        # Every learnable radar is still attempted, and records its own failure.
        for key in "abc":
            attempt = self.runtime.data["devices"][key]["nightly_learning"]
            assert attempt["status"] == "error"
            assert attempt["error"] == "registry broken"
        assert self.runtime.async_save.await_count == 2

    async def test_zone_joining_the_set_during_prepare_reports_changed(self):
        set_id = self.linked()
        self.runtime.data["devices"]["d"] = deepcopy(self.device)
        self.values["d"] = {"g0_move": 20}

        async def join(runtime, key):
            if key == "a":
                groups.configure(self.runtime, None, "Hall", "office", ["c", "d"])

        first, second = self.learning_patches(AsyncMock(side_effect=join))
        with first, second, self.assertRaises(ValueError) as error:
            learning.start_set(self.runtime, set_id)
            await self.runtime._room_learning_jobs[set_id]
        assert str(error.exception) == CHANGED
        assert self.runtime.data["room_learning"][set_id]["job"]["error"] == CHANGED

    async def test_member_paused_during_prepare_reports_changed(self):
        set_id = self.linked()

        async def pause(runtime, key):
            self.runtime.data["devices"]["c"]["recording_enabled"] = False

        first, second = self.learning_patches(AsyncMock(side_effect=pause))
        with first, second, self.assertRaises(ValueError) as error:
            learning.start_set(self.runtime, set_id)
            await self.runtime._room_learning_jobs[set_id]
        assert str(error.exception) == CHANGED
        assert "slots" not in self.runtime.data["room_learning"][set_id]

    async def test_fit_time_zone_check_tolerates_summation_noise(self):
        set_id = self.linked()
        learned = await self.learn(set_id)
        zone = learned["groups"][self.ids["Sofa"]]
        zone["cost_after"] = zone["cost_before"] + 1e-12
        report, writes = await self.automatic(learned)
        # Not refused as worse: it reaches the fresh comparison, which finds no gain.
        assert report["status"] == "skipped"
        assert "does not reduce" in report["reason"]
        assert writes == []

    async def test_paused_member_refuses_with_existing_message(self):
        from tuner_under_test.calibration import jobs as device_jobs

        set_id = self.linked()
        self.runtime.data["devices"]["c"]["recording_enabled"] = False
        names = " + ".join(
            name for _key, name in sorted((key, name) for name, key in self.ids.items())
        )
        for start in (
            lambda: learning.start_set(self.runtime, set_id),
            lambda: learning.start_set(self.runtime, self.ids["Desk"]),
            lambda: device_jobs.start(self.runtime, "a"),
        ):
            with self.assertRaises(ValueError) as error:
                start()
            assert str(error.exception) == f"{names}: {PAUSED}"
        assert set_id not in self.runtime.data.get("room_learning", {})
        assert not self.runtime._room_learning_jobs
        assert not self.runtime._learning_jobs

    async def test_learn_set_and_apply_set_resolve_a_member_zone_id(self):
        set_id = self.linked()
        first, second = self.learning_patches()
        with first, second:
            report = self.runtime.learn_set(self.ids["Sofa"])
            assert report["group_id"] == set_id
            assert list(self.runtime._room_learning_jobs) == [set_id]
            await self.runtime._room_learning_jobs[set_id]
        learned = self.runtime.data["room_learning"][set_id]["slots"]["user"]
        assert self.ids["Sofa"] not in self.runtime.data["room_learning"]
        learned["thresholds"]["a"] = {"g0_move": 30}
        writes, (prepare, write) = self.writer()
        with prepare, write:
            result = await self.runtime.apply_set(self.ids["Desk"], learned["id"], "user")
        assert not result["skipped"]
        assert ("a", "g0_move", 30) in writes
        assert self.runtime.data["room_learning"][set_id]["application"]["status"] == "applied"

    async def test_set_result_goes_stale_when_a_new_zone_joins_the_set(self):
        set_id = self.linked()
        learned = await self.learn(set_id)
        self.runtime.data["devices"]["d"] = deepcopy(self.device)
        self.values["d"] = {"g0_move": 20}
        groups.configure(self.runtime, None, "Hall", "office", ["c", "d"])
        writes, (prepare, write) = self.writer()
        with prepare, write, self.assertRaises(ValueError) as error:
            await self.runtime.apply_set(set_id, learned["id"], "user")
        assert str(error.exception) == (
            "The learning set's zones or membership changed; learn again"
        )
        assert writes == []

    async def test_zone_edit_mid_job_reports_changed_even_with_the_same_radars(self):
        set_id = self.linked()
        real = fitting.fit_set

        def rename_desk(found, *args, **kwargs):
            # Same zones and radars, but a zone definition changed while the fit ran.
            groups.configure(self.runtime, self.ids["Desk"], "Study", "office", ["a", "b"])
            return real(found, *args, **kwargs)

        with patch.object(fitting, "fit_set", rename_desk), self.assertRaises(ValueError) as error:
            await self.learn(set_id)
        assert str(error.exception) == CHANGED
        assert self.runtime.data["room_learning"][set_id]["job"]["error"] == CHANGED
        assert "slots" not in self.runtime.data["room_learning"][set_id]

    def sensor_cycle(self, group_id, start):
        """A room presence sensor attached to ``group_id`` turns on, then off: one sensor
        label opens and is finalised, which retrains the members' references."""
        from tuner_under_test.rooms import room_sources

        entity = "binary_sensor.room_camera"
        if group_id not in self.runtime.data.get(room_sources.STORE, {}):
            sensor = {"entity_id": entity, "kind": "boolean", "area": ""}
            room_sources.configure_room_sources(
                self.runtime,
                group_id,
                {
                    "sources": [sensor],
                    "mark_not_present": False,
                    "confidence": 80,
                    "start_buffer_seconds": 0,
                    "end_buffer_seconds": 0,
                },
            )
        for state, ticks in (("on", range(start, start + 12, 2)), ("off", [start + 12])):
            self.states[entity] = types.SimpleNamespace(state=state, attributes={})
            for now in ticks:
                room_sources.sample_rooms(self.runtime, now)

    async def test_room_sensor_labels_stale_assessments_but_not_the_set_result(self):
        # Deliberate change (final review I2): room sensor activity used to make every set
        # result stale. Like device-level sources, it now leaves learn results applicable;
        # only assessments (spec 6) go stale.
        from tuner_under_test.rooms import jobs

        set_id = self.linked()
        sofa = self.ids["Sofa"]
        # Configure the source before the fit; the later cycles change its observed
        # state, while retaining the source settings that owned the learned policy.
        self.sensor_cycle(sofa, 500)
        learned = await self.learn(set_id)
        stored = {"signature": jobs._context(self.runtime, sofa)[1], "report": {"score": 1}}
        self.runtime.data.setdefault("room_assessments", {})[sofa] = stored
        assert jobs.summary(self.runtime, sofa) == {"score": 1}
        revisions = {key: self.runtime.data["devices"][key].get("label_revision") for key in "abc"}
        self.sensor_cycle(sofa, 1000)
        self.sensor_cycle(sofa, 2000)
        assert jobs.summary(self.runtime, sofa)["state"] == "stale"
        assert {
            key: self.runtime.data["devices"][key].get("label_revision") for key in "abc"
        } == revisions
        writes, (prepare, write) = self.writer()
        learned["thresholds"]["a"] = {"g0_move": 25}
        with prepare, write:
            result = await self.runtime.apply_set(set_id, learned["id"], "user")
        assert not result["skipped"]
        assert ("a", "g0_move", 25) in writes

    async def test_changed_room_sensor_configuration_stales_the_set_result(self):
        from tuner_under_test.rooms import room_sources

        set_id = self.linked()
        sofa = self.ids["Sofa"]
        self.sensor_cycle(sofa, 500)
        learned = await self.learn(set_id)
        configured = room_sources.settings(self.runtime, sofa)
        room_sources.configure_room_sources(self.runtime, sofa, {**configured, "confidence": 90})
        writes, (prepare, write) = self.writer()
        with prepare, write, self.assertRaises(ValueError) as error:
            await self.runtime.apply_set(set_id, learned["id"], "user")
        assert str(error.exception) == (
            "A zone member's labels, timing, thresholds or membership changed; learn again"
        )
        assert writes == []

    async def test_room_sensor_transition_during_set_learning_keeps_the_job(self):
        set_id = self.linked()
        sofa = self.ids["Sofa"]
        self.sensor_cycle(sofa, 500)
        assert self.runtime.data["room_labels"][sofa]
        real = fitting.fit_set

        def flip(found, *args, **kwargs):
            self.sensor_cycle(sofa, 1000)  # The sensor opens and closes a label mid-fit.
            return real(found, *args, **kwargs)

        with patch.object(fitting, "fit_set", flip):
            await self.learn(set_id)
        job = self.runtime.data["room_learning"][set_id]["job"]
        assert job["status"] == "completed", job.get("error")

    async def test_manual_room_label_still_makes_the_set_result_stale(self):
        from tuner_under_test.rooms import room_labels

        set_id = self.linked()
        learned = await self.learn(set_id)
        room_labels.label_room_range(self.runtime, self.ids["Sofa"], 0, 600, "present")
        writes, (prepare, write) = self.writer()
        with prepare, write, self.assertRaises(ValueError) as error:
            await self.runtime.apply_set(set_id, learned["id"], "user")
        assert str(error.exception) == (
            "A zone member's labels, timing, thresholds or membership changed; learn again"
        )
        assert writes == []

    async def test_legacy_one_group_entry_is_read_as_its_set(self):
        self.radars()
        learned = await self.learn("area:office")
        for key in ("group_ids", "groups", "timing"):
            learned.pop(key, None)  # As stored before learning sets existed.
        report, writes = await self.automatic(learned)
        assert report["status"] == "skipped"
        assert "does not reduce" in report["reason"]
        learned["thresholds"]["a"] = {"g0_move": 25}
        writes, (prepare, write) = self.writer()
        with prepare, write:
            result = await self.runtime.apply_set("area:office", learned["id"], "user")
        assert not result["skipped"]
        assert ("a", "g0_move", 25) in writes
