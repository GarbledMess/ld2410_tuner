"""The "Automatic evidence weight" setting: one accessor, blended into both learners' primary cost.

Each automatic observation counts ``weight`` x its confidence of a human one; with human
evidence for a class its share is capped at ``AUTO_CLASS_CAP``. Weight 0 is the pre-blend
learner exactly.
"""

import json
import math
import random
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import automatic_scenarios as scenarios
import pytest
import test_characterisation as characterisation
import test_joint_learning as joint
import test_rooms
import test_tuner as harness
from tuner_under_test.calibration import comparison, service, settings
from tuner_under_test.calibration.constants import AUTO_CLASS_CAP, METHOD
from tuner_under_test.calibration.quality import QUALITY_VERSION
from tuner_under_test.calibration.scoring import automatic_share, blended_cost
from tuner_under_test.history import recording
from tuner_under_test.rooms import application, assessment, fitting, jobs, learning
from tuner_under_test.rooms import objective as objective_module
from tuner_under_test.rooms.objective import LinkedObjective, Objective

DEFAULT = settings.DEFAULT_AUTOMATIC_WEIGHT
OTHER = 0.5 if DEFAULT != 0.5 else 0.3
FROZEN = Path(__file__).parent / "fixtures" / "pre_p2b"


def runtime_with(data=None):
    return types.SimpleNamespace(data=data if data is not None else {}, _schedule_save=Mock())


# Setting and accessor


def test_default_when_unset_and_stored_value_when_set():
    assert settings.automatic_weight({}) == DEFAULT
    assert settings.automatic_weight({"learning_schedule": {"enabled": True}}) == DEFAULT
    assert settings.automatic_weight({"learning_schedule": {"automatic_weight": 0.35}}) == 0.35


def test_default_is_within_the_valid_range():
    assert 0 <= DEFAULT <= 1


@pytest.mark.parametrize("value", [0, 0.0, 0.05, 1, 1.0])
def test_configure_stores_beside_the_schedule_and_saves(value):
    runtime = runtime_with({"learning_schedule": {"enabled": True, "time": "03:00"}})
    result = settings.configure(runtime, value)
    assert runtime.data["learning_schedule"] == {
        "enabled": True,
        "time": "03:00",
        "automatic_weight": float(value),
    }
    assert type(runtime.data["learning_schedule"]["automatic_weight"]) is float
    assert result == {"automatic_weight": float(value), "default_automatic_weight": DEFAULT}
    runtime._schedule_save.assert_called_once_with()


@pytest.mark.parametrize("value", [-0.01, 1.01, math.nan, math.inf, True, "0.2", None])
def test_configure_rejects_values_outside_zero_to_one(value):
    runtime = runtime_with({"learning_schedule": {"enabled": False}})
    with pytest.raises(ValueError, match="from 0 to 1"):
        settings.configure(runtime, value)
    assert runtime.data == {"learning_schedule": {"enabled": False}}
    runtime._schedule_save.assert_not_called()


def test_signature_names_default_and_nondefault_policy_explicitly():
    expected = {
        "automatic_weight": DEFAULT,
        "quality_version": QUALITY_VERSION,
        "learner_method": METHOD,
    }
    assert settings.signature({}) == expected
    assert settings.signature({"learning_schedule": {"automatic_weight": DEFAULT}}) == expected
    assert settings.signature({"learning_schedule": {"automatic_weight": OTHER}}) == {
        **expected,
        "automatic_weight": OTHER,
    }


# Shared blend policy


def test_share_is_weight_times_cap_times_confidence_mass_ratio():
    assert automatic_share(1.0, 100, 90) == pytest.approx(AUTO_CLASS_CAP * 0.9)
    assert automatic_share(0.4, 100, 50) == pytest.approx(0.4 * AUTO_CLASS_CAP * 0.5)
    assert automatic_share(0.0, 100, 90) == 0.0
    assert automatic_share(0.5, 100, 0) == 0.0


def test_share_reaches_the_cap_only_at_full_weight():
    # Plentiful automatic evidence: the mass ratio saturates at 1, the weight never does.
    assert automatic_share(1.0, 10, 5000) == AUTO_CLASS_CAP
    assert automatic_share(0.5, 10, 5000) == AUTO_CLASS_CAP / 2


def test_whole_weight_range_changes_the_share_on_real_data_proportions():
    # ~5000 confident guesses against 800 human rows (the office radars).
    shares = [automatic_share(w, 800, 4500) for w in (0.01, 0.05, 0.1, 0.3, 0.5, 0.75, 1.0)]
    assert shares == sorted(set(shares))
    assert shares[-1] == AUTO_CLASS_CAP


def test_share_without_human_evidence_is_the_weight_times_the_cap():
    assert automatic_share(0.4, 0, 5000) == pytest.approx(0.4 * AUTO_CLASS_CAP)
    assert automatic_share(0.0, 0, 5000) == 0.0


def test_blended_cost_adds_exactly_nothing_at_zero_shares():
    human = 12.3456789012345
    assert blended_cost(human, (0.0, 0.0), (1.0, 1.0)) == human


def test_blended_cost_adds_the_share_weighted_automatic_time_cost():
    # 100 x (5 x missed + false), the human cost formula, on share-scaled automatic rates.
    assert blended_cost(1.0, (0.2, 0.1), (0.5, 0.4)) == pytest.approx(1.0 + 100 * (0.5 + 0.04))


def test_default_weight_is_pinned_with_the_learning_and_scoring_versions():
    # Evidence semantics change learned and saved results even when the default stays zero.
    # Keep METHOD and both SCORER_VERSIONs pinned after the evaluation decision.
    assert (DEFAULT, METHOD, comparison.SCORER_VERSION, assessment.SCORER_VERSION) == (
        0.0,
        "human_priority_v11",
        2,
        2,
    )


# Device learner


def frozen(name):
    return json.loads((FROZEN / f"{name}.json").read_text(encoding="utf-8"))


def test_zero_weight_reproduces_the_native_device_golden():
    result = characterisation.normalise(characterisation.device_fit(automatic_weight=0.0))
    assert result["automatic_evidence"]["base_weight"] == 0.0
    assert without_share_fields(result) == without_share_fields(
        json.loads((characterisation.FIXTURES / "device_fit.json").read_text())
    )


def conflict(slow, weight):
    """Human labels prefer detecting ``slow`` rows at 25 (human cost ~5 x slow for ignoring
    them); confident automatic empty evidence at 28 (90% of the human mass) prefers >= 28."""
    rows, keys, guesses = scenarios.device_conflict(slow)
    result = harness.fit(rows, keys, automatic=guesses, automatic_weight=weight)
    return result["proposals"]["g0_move"]["threshold"]


def test_full_weight_automatic_evidence_outweighs_a_small_human_cost():
    # One independent automatic episode at 0.9, corrected by the quality model's conservative
    # prior (0.75) to 0.675, beside one human episode: share 0.25 x 0.675 / 1.675 ~ 0.10, so
    # automatic cost ~10 beats the human cost of ignoring 2 slow rows (~10) but not 3 (~15).
    assert conflict(2, 1.0) >= 28
    assert conflict(3, 1.0) < 25


def test_partial_weight_counts_proportionally():
    # Share 0.3 x 0.225 = 0.0675: automatic cost 6.75 cannot beat human cost ~20.
    assert conflict(4, 0.3) < 25
    assert conflict(4, 0.05) < 25


def test_cap_keeps_a_larger_human_cost_ahead_of_any_weight():
    # 10 slow rows: human cost ~50 beats the capped automatic cost (22.5) even at weight 1.
    assert conflict(10, 1.0) < 25


def parity():
    return frozen("automatic_parity")


def without_share_fields(result):
    result = json.loads(json.dumps(result))
    for field in ("base_weight", "effective_weight", "share"):
        result["automatic_evidence"].pop(field, None)
    return result


def rounded(value):
    """Floats to 9 places, as the characterisation goldens compare them: the room fit's
    automatic seconds are now summed over a guess run, not over each sample's cell, so they
    differ from the frozen sums by float rounding only (264.6 against 264.60000000000014)."""
    if isinstance(value, float):
        return round(value, 9)
    if isinstance(value, dict):
        return {key: rounded(item) for key, item in value.items()}
    if isinstance(value, list):
        return [rounded(item) for item in value]
    return value


@pytest.mark.parametrize("name", sorted(scenarios.DEVICE))
def test_zero_weight_device_fit_with_automatic_evidence_matches_native_baseline(name):
    frozen_case = parity()["device"][name]
    rows, keys, guesses = scenarios.DEVICE[name]()
    result = harness.fit(rows, keys, automatic=guesses, automatic_weight=0.0)
    # A new day-store recording must produce the same audit fingerprint and full report.
    assert result == harness.fit(rows, keys, automatic=guesses, automatic_weight=0.0)
    assert result == frozen_case["result"]
    if name == "conflict_4":
        # Automatic evidence remains a tie-break: all thresholds 5-24 detect every
        # human row, and the reviewed native choice stays fixed.
        assert result["proposals"]["g0_move"]["threshold"] == 17
    thresholds = {key: item["threshold"] for key, item in result["proposals"].items()}
    rank = list(scenarios.device_rank(rows, keys, guesses, thresholds, 0.0))
    # Native rank is frozen directly, including every tie-break key.
    assert rank == frozen_case["rank"]


@pytest.mark.parametrize("name", ["conflict_2", "conflict_6"])
def test_zero_weight_room_fit_with_automatic_evidence_matches_native_baseline(name):
    frozen_case = parity()["room"][name]
    slow = int(name.split("_")[1])
    result = fitting.fit(scenarios.room_conflict(slow), 0, 1200, automatic_weight=0.0)
    assert rounded(json.loads(json.dumps(result))) == rounded(frozen_case["result"])
    if name == "conflict_2":
        assert result["thresholds"]["a"]["g0_move"] < 25
    objective = Objective(scenarios.room_conflict(slow), 0, 1200, automatic_weight=0.0)
    rank = list(objective.rank(result["thresholds"]))
    assert rank == frozen_case["rank"]


def test_automatic_only_fit_is_independent_of_the_weight():
    guesses = [
        (i, {"g0_move": 30 if i < 100 else 5}, "present" if i < 100 else "not_present", 0.65)
        for i in range(200)
    ]
    fits = [harness.fit([], ["g0_move"], automatic=guesses, automatic_weight=w) for w in (0, 1)]
    assert fits[0]["proposals"] == fits[1]["proposals"]
    assert fits[0]["training"] == fits[1]["training"]


def test_automatic_cost_alone_never_sacrifices_human_presence():
    # Human presence only (human cost 0) with weighted automatic cost: whatever the search
    # settles on, it misses none of the human-labelled presence.
    rng = random.Random(27)
    keys = ["g0_move", "g1_still"]

    def values():
        return {key: rng.randint(0, 60) for key in keys}

    rng.choice([0, 0, 60])
    present = [(i, values(), "present") for i in range(60)]
    rng.choice([0, 60])
    guesses = [
        (-500 + i, values(), rng.choice(["present", "not_present"]), 0.9) for i in range(120)
    ]
    result = harness.fit(present, keys, automatic=guesses, automatic_weight=1.0)
    assert result["training"]["duration"]["missed_seconds"] == 0


def test_reported_share_follows_the_setting_and_confidence():
    rows = harness.row_samples({"g0_move": 30}, {"g0_move": 5})
    guesses = [(-100 + i, {"g0_move": 25}, "present", 0.6) for i in range(100)]
    result = harness.fit(rows, ["g0_move"], automatic=guesses, automatic_weight=0.4)
    evidence = result["automatic_evidence"]
    assert evidence["base_weight"] == 0.4
    assert "effective_weight" not in evidence
    # One automatic episode at 0.6, corrected by the quality prior (0.75) to mass 0.45, beside
    # one human episode: m / (h + m) of the capped weight; no empty guesses.
    mass = 0.6 * 0.75
    assert evidence["share"] == {
        "present": pytest.approx(0.4 * AUTO_CLASS_CAP * mass / (1 + mass)),
        "not_present": 0.0,
    }


# Room / set joint objective


def room_conflict(slow):
    return scenarios.room_conflict(slow)


def test_room_objective_zero_weight_ranks_human_cost_first():
    objective = Objective(room_conflict(2), 0, 1200, automatic_weight=0.0)
    thresholds = {"a": {"g0_move": 40}}
    rates = objective.rates(thresholds)
    human = sum(100 * f * r for f, r in zip((5, 1), rates["human"], strict=True))
    assert objective.rank(thresholds)[0] == round(human, 9)


def test_room_fit_weighted_automatic_empty_evidence_counts():
    result = fitting.fit(room_conflict(2), 0, 1200, automatic_weight=1.0)
    assert 30 <= result["thresholds"]["a"]["g0_move"] < 70


def human_and_automatic_costs(objective, thresholds):
    rates = objective.rates(thresholds)
    return [
        sum(100 * f * r for f, r in zip((5, 1), rates[source], strict=True))
        for source in ("human", "automatic")
    ]


def test_room_rank_keeps_human_cost_as_the_second_key_when_weighted():
    objective = Objective(room_conflict(2), 0, 1200, automatic_weight=1.0)
    for value in (20, 40):
        thresholds = {"a": {"g0_move": value}}
        human, automatic = human_and_automatic_costs(objective, thresholds)
        rank = objective.rank(thresholds)
        # Keys: blend, human, automatic-sourced (0 here), full automatic.
        assert (rank[1], rank[2], rank[3]) == (round(human, 9), 0, round(automatic, 9))
        rates = objective.rates(thresholds)["automatic"]
        assert rank[0] == round(blended_cost(human, objective.shares, rates), 9)


def test_equal_blends_are_ordered_by_human_cost():
    # At 20 the radar detects the slow presence but is active in the guessed-empty time; at 40
    # the reverse. Forcing a tie on the blend, lower human cost must win (not automatic).
    objective = Objective(room_conflict(2), 0, 1200, automatic_weight=1.0)
    low, high = {"a": {"g0_move": 20}}, {"a": {"g0_move": 40}}
    (human_low, auto_low), (human_high, auto_high) = (
        human_and_automatic_costs(objective, item) for item in (low, high)
    )
    assert human_low < human_high and auto_low > auto_high
    with patch.object(objective_module, "blended_cost", return_value=7.0):
        assert objective.rank(low) < objective.rank(high)


def test_linked_rank_sums_all_three_cost_keys():
    linked = LinkedObjective({"zone:a": room_conflict(2)}, 0, 1200, automatic_weight=1.0)
    single = Objective(room_conflict(2), 0, 1200, automatic_weight=1.0)
    for value in (20, 40):
        thresholds = {"a": {"g0_move": value}}
        assert linked.rank(thresholds) == single.rank(thresholds)


def test_linked_rank_rounds_every_cost_key_and_sums_the_margins():
    # Three groups whose 9-place costs sum with float noise (0.1 x 3 = 0.30000000000000004).
    linked = LinkedObjective.__new__(LinkedObjective)
    part = types.SimpleNamespace(radars={}, rank=lambda thresholds: (0.1, 0.1, 0.1, 1, 2, 3))
    linked.groups = {"zone:a": part, "zone:b": part, "zone:c": part}
    assert linked.rank({}) == (0.3, 0.3, 0.3, 3, 6, 9)


def test_room_fit_set_uses_the_same_weight():
    groups = {"zone:a": room_conflict(2)}
    assert (
        fitting.fit_set(groups, 0, 1200, automatic_weight=1.0)["thresholds"]
        == fitting.fit(room_conflict(2), 0, 1200, automatic_weight=1.0)["thresholds"]
    )


def test_zero_weight_reproduces_the_native_joint_goldens():
    result = characterisation.normalise(characterisation.joint_fit_examples(automatic_weight=0.0))
    assert result == json.loads((characterisation.FIXTURES / "joint_fit_examples.json").read_text())


# Signatures, jobs and staleness


class SetSignatureTests(unittest.IsolatedAsyncioTestCase):
    setUp = test_rooms.GroupTests.setUp
    asyncTearDown = test_rooms.GroupTests.asyncTearDown
    office = test_rooms.GroupTests.office
    radars = joint.SetRuntimeTests.radars
    zones = joint.SetRuntimeTests.zones
    linked = joint.SetRuntimeTests.linked
    learning_patches = joint.SetRuntimeTests.learning_patches
    learn = joint.SetRuntimeTests.learn

    def signature(self, group_ids):
        return jobs.set_context(self.runtime, group_ids)[1]

    def assert_additive(self, group_ids):
        before = self.signature(group_ids)
        self.runtime.data.setdefault("learning_schedule", {})["automatic_weight"] = DEFAULT
        assert json.dumps(self.signature(group_ids), sort_keys=True) == json.dumps(
            before, sort_keys=True
        )
        assert before["automatic_weight"] == DEFAULT
        assert before["quality_version"] == QUALITY_VERSION
        assert before["learner_method"] == METHOD
        self.runtime.data["learning_schedule"]["automatic_weight"] = OTHER
        changed = self.signature(group_ids)
        assert changed.pop("automatic_weight") == OTHER
        assert changed == {key: value for key, value in before.items() if key != "automatic_weight"}

    def test_one_group_signature_is_unchanged_at_the_default_only(self):
        self.radars()
        self.assert_additive(["area:office"])

    def test_linked_set_signature_is_unchanged_at_the_default_only(self):
        self.linked()
        self.assert_additive(sorted(self.ids.values()))

    def test_snapshot_reports_the_weight_beside_the_schedule(self):
        self.radars()
        self.registry.entities["sensor.a_g0_move_energy"] = types.SimpleNamespace(
            entity_id="sensor.a_g0_move_energy", device_id="a", domain="sensor"
        )
        self.runtime.data.setdefault("learning_schedule", {})["automatic_weight"] = OTHER
        snapshot = self.runtime.snapshot()
        assert snapshot["learning_schedule"]["automatic_weight"] == OTHER
        assert snapshot["learning_schedule"]["default_automatic_weight"] == DEFAULT
        assert snapshot["devices"]["a"]["auto_learning"]["weight"] == OTHER

    async def test_set_learn_passes_the_setting_to_the_fit(self):
        set_id = self.linked()
        self.runtime.data.setdefault("learning_schedule", {})["automatic_weight"] = OTHER
        with patch.object(learning.fitting, "fit_set", wraps=fitting.fit_set) as fit_set:
            learned = await self.learn(set_id)
        assert fit_set.call_args.args[4] == OTHER
        assert learned["signature"]["automatic_weight"] == OTHER

    async def test_changing_the_weight_makes_a_saved_set_result_stale(self):
        set_id = self.linked()
        learned = await self.learn(set_id)
        application.guard(self.runtime, learned, {})
        self.runtime.data.setdefault("learning_schedule", {})["automatic_weight"] = OTHER
        with pytest.raises(ValueError, match="learn again"):
            application.guard(self.runtime, learned, {})


class DeviceLearnTests(unittest.IsolatedAsyncioTestCase):
    setUp = harness.RuntimeTests.setUp
    asyncTearDown = harness.RuntimeTests.asyncTearDown
    configuration = harness.RuntimeTests.configuration

    def labelled(self):
        self.configuration()
        row = {40: bytes([40] + [255] * 17 + [0, 0]), 5: bytes([5] + [255] * 17 + [0, 0])}
        # 100 present and 100 empty samples, each six seconds of recording.
        start = self.now - 1300
        harness.record_rows(
            self.runtime, "a", [(start + i * 6, row[40 if i < 100 else 5]) for i in range(200)]
        )
        self.device["history_labels"] = [
            {"start": start, "end": start + 600, "state": "present"},
            {"start": start + 600, "end": start + 1200, "state": "not_present"},
        ]
        return self.runtime._threshold_configuration("a")

    def test_default_device_result_records_the_complete_policy_signature(self):
        entities, current = self.labelled()
        learned = self.runtime._fit_history("a", entities, current)
        assert learned["automatic_weight"] == DEFAULT
        assert learned["quality_version"] == QUALITY_VERSION
        assert learned["learner_method"] == METHOD
        assert learned["automatic_evidence"]["base_weight"] == DEFAULT

    def test_device_result_records_a_non_default_weight_through_the_view(self):
        entities, current = self.labelled()
        self.runtime.data["learning_schedule"] = {"automatic_weight": OTHER}
        view = recording._history_view(self.runtime, "a")
        learned = view._fit_history("a", entities, current)
        assert learned["automatic_weight"] == OTHER
        assert learned["automatic_evidence"]["base_weight"] == OTHER

    async def test_weight_change_during_a_device_learn_is_detected(self):
        self.runtime.data["learning_schedule"] = {"automatic_weight": DEFAULT}

        async def executor(fn, *args):
            self.runtime.data["learning_schedule"]["automatic_weight"] = OTHER
            return {}

        self.worker_transport.respond = executor
        with (
            patch.object(service, "read_timing", return_value={}),
            patch.object(service.recovery, "prepare_learning", AsyncMock(return_value=({}, {}))),
            pytest.raises(service.LearningEvidenceChanged, match="weight"),
        ):
            await service._learn_once(self.runtime, "a")

    def test_runtime_configures_and_snapshot_reports_the_setting(self):
        result = self.runtime.configure_automatic_weight(OTHER)
        assert result == {"automatic_weight": OTHER, "default_automatic_weight": DEFAULT}
        assert self.runtime.data["learning_schedule"]["automatic_weight"] == OTHER
        summary = self.runtime.auto_learning_summary(self.device, OTHER)
        assert summary["weight"] == OTHER
        assert f"weight {OTHER:g}," in summary["note"]
        assert "up to a quarter of the human evidence" in summary["note"]
        assert "only breaks ties" in summary["note"]
        assert self.runtime.auto_learning_summary(self.device, 0.0)["weight"] == 0.0
