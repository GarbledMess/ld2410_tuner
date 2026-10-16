"""Corrected room objectives keep physical human replay and independent support."""

import test_rooms  # noqa: F401
from test_rooms import labels, member, set_records
from tuner_under_test.calibration.quality import learn
from tuner_under_test.calibration.quality_evidence import collect
from tuner_under_test.rooms import evidence
from tuner_under_test.rooms.objective import LinkedObjective, Objective


def contexts(members, end=1200):
    return {
        key: collect(item["view"], key, item["thresholds"], start=0, end=end)
        for key, item in members.items()
    }


def test_unsupported_automatic_hit_does_not_hide_physical_false_activation():
    item = member("arbitrary", [10] * 200, labels())
    model = learn(contexts({"arbitrary": item})["arbitrary"])
    objective = Objective({"arbitrary": item}, 0, 1200, quality={"arbitrary": model})
    radar = objective.radars["arbitrary"]
    assert radar.bits({"g0_move": 0}) != 0
    assert radar.automatic_bits({"g0_move": 0}) == 0
    assert objective.rates({"arbitrary": {"g0_move": 0}})["human"][1] == 1


def test_linked_groups_share_unique_corrected_radar():
    item = member("renamed", [70] * 100 + [10] * 100, labels())
    quality = contexts({"renamed": item})
    objective = LinkedObjective(
        {"left": {"renamed": item}, "right": {"renamed": item}}, 0, 1200, quality=quality
    )
    assert objective.groups["left"].radars["renamed"] is objective.groups["right"].radars["renamed"]


def test_automatic_only_corrected_room_learns_nontrivial_threshold():
    from tuner_under_test.rooms import fitting

    item = member("new-name", [], [])
    set_records(
        item["view"],
        "new-name",
        [
            (i * 6, [40 if i < 100 else 0] + [255] * 17 + [1 if i < 100 else 2, 100])
            for i in range(200)
        ],
    )
    item["thresholds"]["g0_move"] = 60
    members = {"new-name": item}
    result = fitting.fit(members, 0, 1200, automatic_weight=0.5, quality_contexts=contexts(members))
    assert 0 < result["thresholds"]["new-name"]["g0_move"] < 40
    assert not result["automatic_evidence"]["selection"]["validated"]


def test_room_explicit_empty_supplies_support_but_estimate_never_doubles_trials():
    from tuner_under_test.rooms.adaptive import collect_contexts, episode_support, models

    item = member("radar", [10] * 200, [])
    room = {
        "group_id": "any-zone",
        "groups": {},
        "room_labels": {
            "any-zone": [{"start": 0, "end": 600, "state": "not_present", "source": "manual"}]
        },
    }
    set_records(item["view"], "radar", [(i * 6, [10] + [255] * 17 + [1, 100]) for i in range(200)])
    quality = models(collect_contexts({"any-zone": {"radar": item}}, 0, 1200, {"any-zone": room}))
    assert quality["radar"].reliability(0, "present") == 3 / 5
    assert not quality["radar"].supports("g0_move", 10)
    assert episode_support(quality, {"radar": item}, "present") == (0, 3 / 5)


def test_other_member_still_covers_union_when_weak_gate_has_no_positive_support():
    members = {
        "weak": member("weak", [10] * 200, labels()),
        "strong": member("strong", [70] * 100 + [10] * 100, labels()),
    }
    objective = Objective(members, 0, 1200, quality=contexts(members))
    thresholds = {"weak": {"g0_move": 20}, "strong": {"g0_move": 20}}
    # Original duration replay includes the sample-boundary uncertainty.
    assert (
        objective.rates(thresholds)["human"]
        == Objective(members, 0, 1200).rates(thresholds)["human"]
    )
    assert objective.rates(thresholds)["human"][0] < 0.01


def test_room_unknown_stops_prior_fallback():
    from tuner_under_test.rooms.adaptive import collect_contexts

    item = member("name", [40] * 200, [])
    set_records(item["view"], "name", [(i * 6, [40] + [255] * 17 + [1, 100]) for i in range(200)])
    room = {
        "group_id": "excluded",
        "groups": {},
        "room_labels": {
            "excluded": [{"start": 0, "end": 1200, "state": "unknown", "source": "manual"}]
        },
    }
    quality = collect_contexts({"excluded": {"name": item}}, 0, 1200, {"excluded": room})
    assert quality["name"].episodes == ()
    assert quality["name"].automatic_episodes == ()


def test_automatic_only_results_are_numerically_invariant_to_arbitrary_renames():
    from tuner_under_test.rooms import fitting

    results = []
    for key in ("anonymous", "different-identifier"):
        item = member(key, [], [])
        set_records(
            item["view"],
            key,
            [
                (i * 6, [40 if i < 100 else 0] + [255] * 17 + [1 if i < 100 else 2, 100])
                for i in range(200)
            ],
        )
        item["thresholds"]["g0_move"] = 60
        members = {key: item}
        result = fitting.fit(
            members, 0, 1200, automatic_weight=0.5, quality_contexts=contexts(members)
        )
        results.append(
            (
                result["thresholds"][key],
                result["cost_after"],
                result["automatic_evidence"]["quality"][key]["automatic_episode_mass"],
            )
        )
    assert results[0] == results[1]


def test_corrected_automatic_bound_is_valid_for_physical_and_supported_masks():
    item = member(
        "bounded",
        [],
        [
            {"start": 0, "end": 600, "state": "present"},
            {"start": 600, "end": 1200, "state": "not_present"},
        ],
    )
    values = [70] * 100 + [10] * 100 + [10, 70] * 100
    set_records(
        item["view"],
        "bounded",
        [(i * 6, [value] + [255] * 17 + [1, 100]) for i, value in enumerate(values)],
    )
    members = {"bounded": item}
    objective = Objective(members, 0, 2400, automatic_weight=0.5, quality=contexts(members, 2400))
    low, high = {"bounded": {"g0_move": 0}}, {"bounded": {"g0_move": 80}}
    bound = objective.bound(low, high)
    for threshold in range(81):
        rates = objective.rates({"bounded": {"g0_move": threshold}})
        assert all(-1e-12 <= rate <= 1 + 1e-12 for pair in rates.values() for rate in pair)
        costs = objective.cost(rates)
        assert all(floor <= value + 1e-9 for floor, value in zip(bound, costs, strict=True))


def test_chronological_selection_uses_two_candidates_then_final_refit():
    from unittest.mock import patch

    from tuner_under_test.rooms import adaptive, fitting

    states = ["present", "not_present"] * 5
    labelled = [
        {"start": i * 600, "end": (i + 1) * 600, "state": state} for i, state in enumerate(states)
    ]
    item = member(
        "any", [70 if state == "present" else 10 for state in states for _ in range(100)], labelled
    )
    members = {"any": item}
    with patch.object(fitting, "search", wraps=fitting.search) as search:
        result = fitting.fit(
            members,
            0,
            6000,
            automatic_weight=0.5,
            quality_contexts=adaptive.collect_contexts({"x": members}, 0, 6000),
        )
    assert search.call_count == 3
    selection = result["automatic_evidence"]["selection"]
    assert selection["validated"]
    assert selection["policy"] == "reference"
    assert set(selection["candidates"]) == {"reference", "corrected_positive"}


def test_independent_sensor_without_recorded_owner_keeps_prior_and_no_manual_trials():
    from tuner_under_test.rooms.adaptive import collect_contexts

    item = member("unique", [40] * 100 + [0] * 100, [])
    room = {
        "group_id": "sensor-zone",
        "groups": {},
        "room_labels": {
            "sensor-zone": [
                {"start": 0, "end": 600, "state": "present", "source": "sensor", "confidence": 0.9},
                {
                    "start": 600,
                    "end": 1200,
                    "state": "not_present",
                    "source": "sensor",
                    "confidence": 0.9,
                },
            ]
        },
    }
    quality = collect_contexts({"sensor-zone": {"unique": item}}, 0, 1200, {"sensor-zone": room})
    objective = Objective({"unique": item}, 0, 1200, room, quality=quality)
    assert quality["unique"].episodes == ()
    assert objective.truth["sensor"](100) == ("present", "automatic", 0.9 * 0.75)


def test_moved_held_room_labels_do_not_change_earlier_quality_or_thresholds():
    from copy import deepcopy

    from tuner_under_test.rooms import adaptive, fitting

    # Ten alternating 600-second room episodes; latest two form validation.
    item = member("radar", [70 if i % 2 == 0 else 10 for i in range(10) for _ in range(100)], [])
    room = {
        "group_id": "zone",
        "groups": {},
        "room_labels": {
            "zone": [
                {
                    "start": i * 600,
                    "end": (i + 1) * 600,
                    "state": "present" if i % 2 == 0 else "not_present",
                    "source": "manual",
                }
                for i in range(10)
            ]
        },
    }
    changed = deepcopy(room)
    changed["room_labels"]["zone"][-1]["start"] += 12
    groups = {"zone": {"radar": item}}
    selections = []
    for source in (room, changed):
        rooms = {"zone": source}
        result = fitting.fit_set(
            groups,
            0,
            6000,
            rooms,
            0.5,
            quality_contexts=adaptive.collect_contexts(groups, 0, 6000, rooms),
        )
        selections.append(result["automatic_evidence"]["selection"])
    assert selections[0]["cutoff"] == selections[1]["cutoff"]
    assert selections[0]["training_quality"] == selections[1]["training_quality"]
    assert [item["thresholds"] for item in selections[0]["candidates"].values()] == [
        item["thresholds"] for item in selections[1]["candidates"].values()
    ]


def test_supported_automatic_masks_match_independent_scalar_gate_rule():
    item = member(
        "independent",
        [],
        [
            {"start": 0, "end": 600, "state": "present"},
            {"start": 600, "end": 1200, "state": "not_present"},
        ],
    )
    values = [70] * 100 + [10] * 200
    set_records(
        item["view"],
        "independent",
        [(i * 6, [value] + [255] * 17 + [1, 100]) for i, value in enumerate(values)],
    )
    members = {"independent": item}
    objective = Objective(members, 0, 1800, quality=contexts(members, 1800), automatic_weight=0.5)
    radar = objective.radars["independent"]
    # Each recorded row is a span of one reading (the 70s, then the 10s).
    found = evidence.read(item["view"], "independent", ["g0_move"], 0, 1800)["rows"]
    energies = [row[1]["g0_move"] for row in found]
    assert sorted(set(energies)) == [10, 70]
    for threshold in range(101):
        # One occupied episode observed 70; empty observed 10. The quiet bin
        # lacks two occupied episodes; 70 is above the empty ceiling.
        expected = sum(
            1 << i for i, energy in enumerate(energies) if energy > threshold and energy > 10
        )
        assert radar.automatic_bits({"g0_move": threshold}) == expected
        physical = sum(1 << i for i, energy in enumerate(energies) if energy > threshold)
        assert radar.bits({"g0_move": threshold}) == physical
    thresholds = {"independent": {"g0_move": 0}}
    assert objective.rates(thresholds)["automatic"][0] == 1
    assert objective.rates(thresholds)["human"][1] == 1


def test_room_manual_episode_has_one_share_even_with_two_member_contexts():
    from tuner_under_test.rooms.adaptive import collect_contexts

    members = {key: member(key, [70] * 100 + [10] * 100, []) for key in ("a", "b")}
    room = {
        "group_id": "shared",
        "groups": {},
        "room_labels": {
            "shared": [
                {"start": 0, "end": 600, "state": "present", "source": "manual"},
                {"start": 600, "end": 1200, "state": "not_present", "source": "manual"},
            ]
        },
    }
    quality = collect_contexts({"shared": members}, 0, 1200, {"shared": room})
    objective = Objective(members, 0, 1200, room, quality=quality)
    assert objective._support("present") == (1, 0)
    assert objective._support("not_present") == (1, 0)


def test_production_worker_passes_quality_once_per_unique_member():
    from unittest.mock import patch

    from tuner_under_test.rooms import adaptive, learning

    item = member("same", [70] * 100 + [10] * 100, labels())
    with patch.object(adaptive, "collect", wraps=adaptive.collect) as collect_context:
        result = learning._fit_set(
            {"left": {"same": item}, "right": {"same": item}}, 0, 1200, {}, 0.5
        )
    assert collect_context.call_count == 1
    assert set(result["automatic_evidence"]["quality"]) == {"same"}


def test_room_installation_rejects_rewritten_recorded_history():
    import tempfile
    from copy import deepcopy
    from pathlib import Path
    from types import SimpleNamespace

    import pytest
    from tuner_under_test.history import dayfile, daystore
    from tuner_under_test.rooms import learning

    item = member("owned", [], [])
    with tempfile.TemporaryDirectory() as folder:
        days = daystore.DayStore(Path(folder))
        days.append("owned", dayfile.Change(0, {0: 40}))
        days.flush(now=86401)  # The first day is finished.
        runtime = SimpleNamespace(data=deepcopy(item["view"].data), _history_runtime={}, _days=days)
        item["view"]._history_rewrites = {"owned": days.rewrites("owned")}
        learning._check_history(runtime, {"group": {"owned": item}})
        days.delete("owned", [daystore.date_of(0)])  # Retention or Trim now, during the job.
        with pytest.raises(ValueError, match=learning.CHANGED):
            learning._check_history(runtime, {"group": {"owned": item}})


def test_later_supported_presence_can_select_useful_positive_room_policy():
    from tuner_under_test.rooms import fitting

    states = ["present", "not_present"] * 4 + [None, "present", "not_present"]
    labelled = [
        # Leave the three-second sample-cell transition uncertainty unlabelled.
        # This case tests a new presence regime above observed quiet energy;
        # quiet-overlapping regimes require independent occupied support.
        {"start": i * 600 + 3, "end": (i + 1) * 600 - 3, "state": state}
        for i, state in enumerate(states)
        if state is not None
    ]
    item = member("useful", [], labelled)
    records = []
    for i, state in enumerate(states):
        energy = 90 if i < 8 and state == "present" else 10 if state == "not_present" else 40
        records.extend(
            (i * 600 + j * 6, [energy] + [255] * 17 + [2 if state == "not_present" else 1, 100])
            for j in range(100)
        )
    set_records(item["view"], "useful", records)
    item["thresholds"]["g0_move"] = 60
    members = {"useful": item}
    result = fitting.fit(
        members, 0, 6600, automatic_weight=0.5, quality_contexts=contexts(members, 6600)
    )
    selection = result["automatic_evidence"]["selection"]
    assert selection["validated"]
    assert selection["policy"] == "corrected_positive"
    ref, pos = (
        selection["candidates"][key]["score"] for key in ("reference", "corrected_positive")
    )
    assert pos["missed_seconds"] < ref["missed_seconds"]
    assert pos["false_seconds"] == ref["false_seconds"]


def test_only_skipped_member_does_not_block_scorable_set_chronology():
    from tuner_under_test.rooms import adaptive, fitting

    labelled = [
        {
            "start": i * 600,
            "end": (i + 1) * 600,
            "state": "present" if i % 2 == 0 else "not_present",
        }
        for i in range(10)
    ]
    active = member(
        "active", [70 if i % 2 == 0 else 10 for i in range(10) for _ in range(100)], labelled
    )
    unused = member("unused", [10] * 1000, [{"start": 0, "end": 6000, "state": "not_present"}])
    groups = {"scored": {"active": active}, "skipped": {"unused": unused}}
    result = fitting.fit_set(
        groups,
        0,
        6000,
        automatic_weight=0.5,
        quality_contexts=adaptive.collect_contexts(groups, 0, 6000),
    )
    assert result["automatic_evidence"]["selection"]["validated"]
    assert result["thresholds"]["unused"] == unused["thresholds"]
    assert "skipped" in result["groups"]["skipped"]
