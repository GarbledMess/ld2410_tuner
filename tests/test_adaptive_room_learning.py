"""Corrected room objectives keep physical human replay and independent support."""

import test_rooms  # noqa: F401
from test_rooms import labels, member, set_records
from tuner_under_test.calibration.quality import learn
from tuner_under_test.calibration.quality_evidence import collect
from tuner_under_test.engine.hybrid import adapters, fixed, replay
from tuner_under_test.rooms import adaptive


def contexts(members, end=1200):
    return {
        key: collect(item["view"], key, item["thresholds"], start=0, end=end)
        for key, item in members.items()
    }


def test_room_fit_does_not_select_cutoff_before_requested_start():
    from tuner_under_test.rooms import adaptive, fitting

    item = member(
        "synthetic", [70 if i % 2 == 0 else 10 for i in range(10) for _ in range(100)], []
    )
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
    groups, rooms = {"zone": {"synthetic": item}}, {"zone": room}
    result = fitting.fit_set(
        groups,
        5000,
        6000,
        rooms,
        0.5,
        quality_contexts=adaptive.collect_contexts(groups, 5000, 6000, rooms),
    )
    assert result["automatic_evidence"]["selection"]["cutoff"] is None
    from tuner_under_test.engine.hybrid import adapters, fixed

    # The chronological cut is drawn from the explicit episodes inside the requested window.
    zone = adapters.room_problem(groups, 5000, 6000, rooms, 0.5).zones[0]
    episodes = sorted(fixed.episodes(zone, "zone"), key=lambda ep: ep.start)
    assert [(ep.start, ep.end, ep.state) for ep in episodes] == [
        (5000, 5400, "present"),
        (5400, 6000, "not_present"),
    ]


def test_unsupported_automatic_hit_does_not_hide_physical_false_activation():
    item = member("arbitrary", [10] * 200, labels())
    model = learn(contexts({"arbitrary": item})["arbitrary"])
    problem = adapters.room_problem({"zone": {"arbitrary": item}}, 0, 1200, {}, 0.5)
    tables = replay.support_tables(problem.radars["arbitrary"], model)
    assert replay.TABLES[0][10] == 1
    assert tables[0][0][10] == 0
    with fixed.FixedScores(problem, 1) as scores:
        found = scores.scores([{"arbitrary": {0: 0}}], problem.zones)[0]["zone"]
    assert found["false_active_seconds"] == found["empty_seconds"]


def test_linked_groups_share_unique_corrected_radar():
    from unittest.mock import patch

    item = member("renamed", [70] * 100 + [10] * 100, labels())
    with patch.object(adapters, "CapturedRowSource", wraps=adapters.CapturedRowSource) as rows:
        problem = adapters.room_problem(
            {"left": {"renamed": item}, "right": {"renamed": item}}, 0, 1200, {}, 0.5
        )
    assert rows.call_count == 1
    assert set(problem.rows) == set(problem.radars) == {"renamed"}
    assert all(zone.members == ("renamed",) for zone in problem.zones)
    assert all(len(block.times) <= 4096 for block in problem.rows["renamed"].blocks(0, 1200))


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
    problem = adapters.room_problem({"zone": members}, 0, 1200, {}, 0.5)
    pattern = {key: {0: 20} for key in members}
    with fixed.FixedScores(problem, 1) as scores:
        found = scores.scores([pattern], problem.zones)[0]["zone"]
    assert found["occupied_recall"] == 1
    assert found["false_active_seconds"] == 0
    assert found["exclusive_seconds"]["strong"] == found["occupied_seconds"]
    assert found["exclusive_seconds"]["weak"] == 0


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
    item = member("bounded", [], labels())
    values = [70] * 100 + [10] * 100 + [10, 70] * 100
    set_records(
        item["view"],
        "bounded",
        [(i * 6, [value] + [255] * 17 + [1, 100]) for i, value in enumerate(values)],
    )
    problem = adapters.room_problem({"zone": {"bounded": item}}, 0, 2400, {}, 0.5)
    automatic = fixed.source(problem.zones[0], "automatic")
    patterns = [{"bounded": {0: threshold}} for threshold in range(81)]
    with fixed.FixedScores(problem, len(patterns)) as scores:
        found = scores.scores(patterns, [problem.zones[0], automatic])
    for measured in found:
        for metrics in measured.values():
            assert 0 <= (metrics["occupied_recall"] or 0) <= 1
            assert 0 <= metrics["false_active_seconds"] <= metrics["empty_seconds"]
            for total, error, episodes, failed in metrics["sufficient_statistics"].values():
                assert 0 <= error <= total
                assert 0 <= failed <= episodes


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
    from tuner_under_test.engine.hybrid import engine

    with patch.object(engine, "fit", wraps=engine.fit) as search:
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
    problem = adapters.room_problem(
        {"sensor-zone": {"unique": item}}, 0, 1200, {"sensor-zone": room}, 0.5
    )
    assert quality["unique"].episodes == ()
    span = next(s for s in problem.zones[0].truth if s.start <= 100 < s.end)
    assert (span.state, span.source, span.confidence) == ("present", "automatic", 0.9 * 0.75)


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
    item = member("independent", [], labels())
    values = [70] * 100 + [10] * 200
    set_records(
        item["view"],
        "independent",
        [(i * 6, [value] + [255] * 17 + [1, 100]) for i, value in enumerate(values)],
    )
    problem = adapters.room_problem({"zone": {"independent": item}}, 0, 1800, {}, 0.5)
    model = problem.policy.quality["models"]["independent"]
    tables = replay.support_tables(problem.radars["independent"], model)
    for threshold in range(101):
        for energy in (10, 70, 255):
            assert bool(tables[0][threshold][energy]) == (
                energy != 255 and energy > threshold and energy > 10
            )
            assert bool(replay.TABLES[threshold][energy]) == (energy != 255 and energy > threshold)
    human, automatic = (fixed.source(problem.zones[0], source) for source in ("human", "automatic"))
    with fixed.FixedScores(problem, 1) as scores:
        found = scores.scores([{"independent": {0: 0}}], [human, automatic])[0]
    assert found[automatic.group_id]["sufficient_statistics"]["present/automatic"][1] > 0
    assert found[human.group_id]["false_active_seconds"] == found[human.group_id]["empty_seconds"]


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
    problem = adapters.room_problem(
        {"shared": members},
        0,
        1200,
        {"shared": room},
        0.5,
        quality_contexts=adaptive.models(quality),
    )
    support = problem.policy.quality["zones"]["shared"]
    assert support["human_episodes"] == {"present": 1, "not_present": 1}
    assert support["automatic_episode_mass"] == {"present": 0, "not_present": 0}


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
    assert "unused" not in result["automatic_evidence"]["quality"]
    assert result["thresholds"]["unused"] == unused["thresholds"]
    assert "skipped" in result["groups"]["skipped"]


def test_finished_policy_replays_are_freed_before_the_next_search(monkeypatch):
    from tuner_under_test.engine.hybrid import engine
    from tuner_under_test.rooms import fitting

    states = ["present", "not_present"] * 5
    labelled = [
        {"start": i * 600, "end": (i + 1) * 600, "state": state} for i, state in enumerate(states)
    ]
    members = {
        "synthetic": member(
            "synthetic",
            [70 if state == "present" else 10 for state in states for _ in range(100)],
            labelled,
        )
    }
    files, searches = [], []
    original_records, original_fit = replay.Records.__init__, engine.fit

    def observe_records(self, *args, **kwargs):
        original_records(self, *args, **kwargs)
        files.append(self.file)

    def observe_fit(*args, **kwargs):
        assert all(file.closed for file in files)
        searches.append(True)
        return original_fit(*args, **kwargs)

    monkeypatch.setattr(replay.Records, "__init__", observe_records)
    monkeypatch.setattr(engine, "fit", observe_fit)
    result = fitting.fit(
        members,
        0,
        6000,
        automatic_weight=0.5,
        quality_contexts=adaptive.collect_contexts({"x": members}, 0, 6000),
    )
    assert result["automatic_evidence"]["selection"]["validated"]
    assert len(searches) == 3
    assert files and all(file.closed for file in files)


def test_linked_measurement_uses_each_zones_independent_support(monkeypatch):
    from dataclasses import replace

    from tuner_under_test.engine.hybrid import evaluate

    item = member(
        "shared",
        [],
        [
            {"start": 0, "end": 600, "state": "present"},
            {"start": 1200, "end": 1800, "state": "not_present"},
        ],
    )
    set_records(
        item["view"],
        "shared",
        [
            (
                i * 6,
                [70 if i < 100 else 10 if i < 200 else 0] + [255] * 17 + [1 if i < 200 else 2, 100],
            )
            for i in range(300)
        ],
    )
    problem = adapters.room_problem(
        {"left": {"shared": item}, "right": {"shared": item}}, 0, 1800, {}, 1
    )
    support = {
        "human_episodes": {"present": 1, "not_present": 1},
        "automatic_episode_mass": {"present": 0, "not_present": 0},
    }
    quality = {**problem.policy.quality, "zones": dict.fromkeys(("left", "right"), support)}
    problem = replace(problem, policy=replace(problem.policy, quality=quality))
    zones = [*problem.zones, fixed.variant(problem.zones[0], "report", lambda *_: True)]
    for budget, expected_mode in ((30_000_000, "exact"), (0, "cells")):
        monkeypatch.setattr(evaluate, "EXACT_BUDGET", budget)
        with fixed.FixedScores(problem, 1) as scores:
            assert scores.mode == expected_mode
            measured = scores.scores([{"shared": {0: 20}}], zones)[0]
        assert {metrics["cost"] for metrics in measured.values()} == {0}
