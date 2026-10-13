"""Persistent, correctable reference learning under changing room conditions."""

import json
import sys
import types
import unittest
from collections import Counter
from copy import deepcopy
from unittest.mock import AsyncMock, patch

import pytest
import test_rooms as rooms_harness
import test_sources as source_harness
import test_tuner as harness
from tuner_under_test.calibration import (
    comparison,
    evidence,
    fitting,
    scoring,
    service,
    settings,
)
from tuner_under_test.history import recording
from tuner_under_test.rooms import attribution, groups, room_labels

refs = sys.modules["tuner_under_test.presence.references"]
training = sys.modules["tuner_under_test.presence.reference_training"]
inference = sys.modules["tuner_under_test.presence.inference"]
cleanup = sys.modules["tuner_under_test.history.cleanup"]
budget = sys.modules["tuner_under_test.history.budget"]
NOW = 2_000_000_000
# Quiet samples that give a rule-2 attribution confidence of 0.6, above MIN_AUTO_CONFIDENCE.
QUIET = 60


def teach(profile, state, energy, source="human", start=NOW, count=60, confidence=1):
    for index in range(count):
        refs.observe(profile, {"g0_still": energy}, state, source, confidence, start + index * 6)


def estimate(profile, energy, now):
    manual, cap = refs.distributions(profile, now)
    temporal, confirmation = {}, {}
    for step in range(30):
        result = inference.estimate_presence(
            {"g0_still": energy}, manual, {}, temporal, now + step * 2, ["g0_still"]
        )
        state = inference.confirm_estimate(result, confirmation, now + step * 2)
    return state, min(result["confidence"], cap)


def seeded():
    profile = refs.empty()
    teach(profile, "not_present", 8)
    teach(profile, "present", 70, start=NOW + 600)
    return profile


def test_new_entity_background_corrects_fan_without_losing_quiet_presence():
    profile = seeded()
    later = NOW + 30 * refs.DAY
    assert estimate(profile, 25, later)[0] == "present"
    assert refs.unfamiliar(profile, {"g0_still": 25}) == ["g0_still"]
    teach(profile, "not_present", 25, "entity", start=later, confidence=0.9)
    assert estimate(profile, 25, later + 600)[0] == "not_present"
    assert estimate(profile, 70, later + 600)[0] == "present"
    assert not refs.unfamiliar(profile, {"g0_still": 25})


def test_recent_human_correction_outweighs_same_age_entity_distribution():
    profile = refs.empty()
    teach(profile, "not_present", 8, start=NOW, count=120)
    teach(profile, "not_present", 25, "entity", start=NOW, count=120)
    reference, _ = refs.distributions(profile, NOW + 720)
    noise = reference["g0_still"]["not_present"]
    assert noise[8] > noise[25]


def test_independent_influence_fades_and_recent_entity_can_overtake_old_human():
    profile = refs.empty()
    teach(profile, "not_present", 8, count=120)
    later = NOW + 30 * refs.DAY
    teach(profile, "not_present", 25, "entity", start=later, count=120, confidence=0.9)
    reference, _ = refs.distributions(profile, later + 720)
    assert reference["g0_still"]["not_present"][25] > reference["g0_still"]["not_present"][8]


def test_repeated_guesses_never_increase_independent_confidence():
    profile = seeded()
    when = NOW + 86400
    _, before = refs.distributions(profile, when + 3600)
    teach(profile, "not_present", 9, "radar", start=when, count=600, confidence=0.98)
    _, after = refs.distributions(profile, when + 3600)
    assert after == before
    # An initially steady occupied room is not an independent empty reference.
    untrained = refs.empty()
    teach(untrained, "not_present", 70, "radar", count=600, confidence=0.98)
    reference, cap = refs.distributions(untrained, NOW + 3600)
    assert not reference
    assert cap == 0.65


def test_quiet_presence_is_not_absorbed_into_background():
    profile = seeded()
    for day in range(1, 40):
        state, _ = estimate(profile, 70, NOW + day * refs.DAY)
        assert state == "present"
        teach(profile, state, 70, "radar", start=NOW + day * refs.DAY, confidence=0.9)
    noise, _ = refs.distributions(profile, NOW + 40 * refs.DAY)
    assert noise["g0_still"]["not_present"][70] == 0


def test_references_survive_retention_and_restart_with_bounded_storage():
    profile = seeded()
    device = {"reference_profile": profile, "history": []}
    updated, _ = cleanup.clean_history(device, [], NOW + 40 * refs.DAY, 30 * refs.DAY)
    device.update(updated)
    restored = json.loads(json.dumps(device))
    assert estimate(restored["reference_profile"], 70, NOW + 40 * refs.DAY)[0] == "present"
    without_profile = budget.storage_bytes(
        {"devices": {"a": {k: v for k, v in device.items() if k != "reference_profile"}}}
    )
    for day in range(100):
        teach(profile, "not_present", 8 + day % 3, "entity", start=NOW + day * refs.DAY)
    assert len([p for p in profile["periods"] if p["source"] == "entity"]) == refs.MAX_PERIODS
    assert budget.storage_bytes({"devices": {"a": device}}) - without_profile < 50_000


def test_profile_updates_do_not_mutate_previously_decoded_periods():
    profile = refs.empty()
    teach(profile, "not_present", 8, count=1)
    first = profile["periods"][0]["data"]
    snapshot = deepcopy(refs.decode(first))
    refs.observe(profile, {"g0_still": 9}, "not_present", "human", 1, NOW + 6)
    assert refs.decode(first) == snapshot
    before = deepcopy(profile)
    refs.observe(profile, {"g0_still": 9}, "not_present", "human", 1, NOW + 6)
    assert profile == before
    refs.observe(profile, {"g0_still": 9}, "unknown", "human", 1, NOW + 12)
    assert profile == before


def test_adapted_guesses_support_new_thresholds_after_human_recordings_expire():
    fitting = sys.modules["tuner_under_test.calibration.fitting"]
    profile = seeded()
    later = NOW + 40 * refs.DAY
    assert estimate(profile, 50, later)[0] == "present"
    teach(profile, "not_present", 50, "entity", start=later, confidence=0.9)
    automatic = []
    for offset, energy in ((0, 50), (600, 70)):
        state, confidence = estimate(profile, energy, later + 600)
        automatic.extend(
            (later + 600 + offset + i * 6, {"g0_still": energy}, state, confidence)
            for i in range(60)
        )
    result = fitting.fit_thresholds([], ["g0_still"], {"g0_still": 39}, automatic)
    threshold = result["proposals"]["g0_still"]["threshold"]
    assert 50 <= threshold < 70
    assert result["evidence_basis"] == "automatic"
    assert result["estimated_training"]["false_positives"] == 0
    assert result["estimated_training"]["false_negatives"] == 0


def test_low_confidence_entities_do_not_create_high_confidence_guesses():
    profile = refs.empty()
    teach(profile, "not_present", 8, "entity", confidence=0.55)
    teach(profile, "present", 70, "entity", start=NOW + 600, confidence=0.55)
    _, cap = refs.distributions(profile, NOW + 1000)
    assert cap <= 0.55


def test_batched_reference_import_matches_live_updates():
    observations = [
        (NOW + i * 6, {"g0_still": i % 30}, "present" if i < 400 else "not_present", "human", 1.0)
        for i in range(800)
    ]
    batch, live = refs.empty(), refs.empty()
    refs.observe_many(batch, observations)
    for timestamp, values, state, source, confidence in observations:
        refs.observe(live, values, state, source, confidence, timestamp)
    assert batch == live


class ReferenceRuntimeTests(unittest.IsolatedAsyncioTestCase):
    setUp = harness.RuntimeTests.setUp
    asyncTearDown = harness.RuntimeTests.asyncTearDown
    sample = harness.RuntimeTests.sample
    entity = source_harness.SourceTests.entity
    configure = source_harness.SourceTests.configure

    async def refresh(self):
        """Rebuild every stale radar's references, as the overnight pass or panel does."""
        from tuner_under_test.presence import rebuilds

        await rebuilds.refresh(self.runtime, reason="test")

    def test_live_human_overrides_disagreeing_entity(self):
        self.configure(negative=True)
        self.entity("binary_sensor.camera_person", "off")
        with patch("time.time", return_value=self.now):
            self.runtime.set_training_state("a", "present")
        for index in range(30):
            self.sample(self.now + index * 6, 70)
        periods = self.device["reference_profile"]["periods"]
        assert {p["source"] for p in periods} == {"human"}
        assert {p["state"] for p in periods} == {"present"}

    def test_entity_labels_teach_guesses_even_after_source_is_unavailable(self):
        self.configure(negative=True)
        for i in range(120):
            self.entity("binary_sensor.camera_person", "off" if i < 60 else "on")
            self.sample(self.now + i * 6, 8 if i < 60 else 70)
        self.entity("binary_sensor.camera_person", "unavailable")
        for i in range(120, 150):
            self.sample(self.now + i * 6, 70)
        last = self.device["auto"]["last_classification"]
        assert last["basis"] == "adaptive-guided"
        assert last["state"] == "present"
        assert self.device.get("histograms", {}) == {}

    async def test_retroactive_correction_replaces_human_and_removes_conflicting_entity_summary(
        self,
    ):
        self.configure(negative=True)
        self.entity("binary_sensor.camera_person", "off")
        for i in range(30):
            self.sample(self.now + i * 6, 70)
        self.runtime.label_history_range("a", self.now, self.now + 180, "present")
        # The conflicting entity summary goes at once; human evidence waits for a refresh.
        assert self.device["reference_profile"]["periods"] == []
        await self.refresh()
        periods = self.device["reference_profile"]["periods"]
        assert {p["source"] for p in periods} == {"human"}
        assert {p["state"] for p in periods} == {"present"}
        self.runtime.label_history_range("a", self.now, self.now + 180, "unknown")
        await self.refresh()
        assert not self.device["reference_profile"]["periods"]

    def test_short_buffered_pulses_never_train_entity_references(self):
        self.runtime.configure_presence_sources(
            "a",
            {"sources": [source_harness.boolean()], "mark_not_present": False, "confidence": 90},
        )
        self.entity("binary_sensor.camera_person", "on")
        for i in range(4):
            self.sample(self.now + i * 6, 70)
        self.entity("binary_sensor.camera_person", "off")
        self.sample(self.now + 20, 70)
        assert not self.device.get("reference_profile", {}).get("periods")

    def test_buffered_entity_training_only_uses_confirmed_interior(self):
        self.runtime.configure_presence_sources(
            "a",
            {"sources": [source_harness.boolean()], "mark_not_present": False, "confidence": 90},
        )
        self.entity("binary_sensor.camera_person", "on")
        for i in range(6):
            self.sample(self.now + i * 6, 70)
        periods = self.device["reference_profile"]["periods"]
        assert len(periods) == 1
        assert periods[0]["count"] == 2
        assert periods[0]["start"] == self.now + 12
        assert periods[0]["end"] == self.now + 18

    def test_source_reconfiguration_drops_entity_models_and_clear_drops_all_models(self):
        self.configure()
        self.entity("binary_sensor.camera_person", "on")
        self.sample(self.now, 70)
        assert self.device["reference_profile"]["periods"]
        self.configure(negative=True)
        assert not self.device["reference_profile"]["periods"]
        self.runtime.clear_samples("a")
        assert "reference_profile" not in self.device

    async def test_existing_human_history_is_imported_once_with_original_times(self):
        self.sample(self.now, 70)
        self.runtime.label_history_range("a", self.now - 1, self.now + 1, "present")
        self.device.pop("reference_profile", None)
        await training.initialize(self.runtime)
        before = deepcopy(self.device["reference_profile"])
        assert before["periods"][0]["end"] == self.now
        await training.initialize(self.runtime)
        assert self.device["reference_profile"] == before
        with patch("time.time", return_value=self.now + 40 * refs.DAY):
            await self.runtime.async_clean_history()
        assert self.device["reference_profile"] == before


def test_legacy_histogram_import_keeps_age_uncertainty_explicit():
    case = ReferenceRuntimeTests()
    case.setUp()
    try:
        histogram = [0] * 101
        histogram[70] = 100
        case.device["history_legacy_histograms"] = {"g0_move": {"present": histogram}}
        training._import_history(case.runtime, case.device)
        profile = case.device["reference_profile"]
        assert profile["periods"][0]["imported_legacy"]
        assert refs.summary(profile, case.now)["legacy_age_unknown"]
        assert refs.summary(profile, case.now)["last_independent_label"] is None
    finally:
        case.doCleanups()


class RoomReferenceTests(unittest.IsolatedAsyncioTestCase):
    """Room labels teach each member radar only what that radar saw (spec 4.3)."""

    setUp = harness.RuntimeTests.setUp
    asyncTearDown = harness.RuntimeTests.asyncTearDown

    def office(self):
        rooms_harness.GroupTests.office(self)
        self.base = self.now - 3600
        self.b = self.runtime.data["devices"]["b"]

    def record(self, a, b, start=0, count=40):
        for i in range(start, start + count):
            when = self.base + i * 6
            self.runtime._record_history_sample("a", {"g0_move": a}, when)
            self.runtime._record_history_sample("b", {"g0_move": b}, when)

    def label(self, start, end, state, group="area:office"):
        room_labels.label_room_range(
            self.runtime, group, self.base + start * 6, self.base + end * 6, state
        )

    def human(self, device):
        periods = device.get("reference_profile", {}).get("periods", [])
        return {p["state"] for p in periods if p["source"] == "human"}

    def inferred(self, device):
        """States the radar learned from room truth as an inference (rule-2 attribution)."""
        periods = device.get("reference_profile", {}).get("periods", [])
        return {p["state"] for p in periods if p.get("room") and p["source"] == "radar"}

    async def refresh(self):
        """Rebuild every stale radar's references, as the overnight pass or panel does."""
        from tuner_under_test.presence import rebuilds

        await rebuilds.refresh(self.runtime, reason="test")

    def test_room_empty_trains_member_empty_reference_live(self):
        self.office()
        self.label(0, 100, "not_present")
        self.record(5, 5)
        assert self.human(self.device) == {"not_present"}
        assert self.human(self.b) == {"not_present"}

    def test_live_room_empty_needs_every_group_of_the_radar_empty(self):
        self.office()
        groups.configure(self.runtime, None, "Overlap", None, ["a", "b"])
        self.label(0, 100, "not_present")
        self.record(5, 5)
        assert self.human(self.device) == set()

    def test_room_empty_of_a_single_radar_group_keeps_live_training_unchanged(self):
        self.office()
        groups.configure(self.runtime, "area:office", "Office", "office", ["b"])
        self.label(0, 100, "not_present")
        self.record(5, 5)
        assert self.human(self.device) == set()
        assert self.human(self.b) == set()

    async def test_room_present_unattributed_never_enters_present_reference(self):
        # Regression: quiet member samples never enter a radar's Present reference.
        self.office()
        self.record(5, 5, count=QUIET)
        self.record(5, 70, start=QUIET)
        self.label(0, QUIET, "not_present")
        self.label(QUIET, QUIET + 40, "present")
        await self.refresh()
        assert self.human(self.device) == {"not_present"}
        assert self.inferred(self.device) == set()
        assert self.human(self.b) == {"not_present"}  # b's rise is rule 2: an inference.
        assert self.inferred(self.b) == {"present"}
        rows, guesses = evidence.collect_samples(self.runtime, "a", ["g0_move"])
        assert {row[2] for row in rows} == {"not_present"}
        assert guesses == []

    async def test_attributed_room_present_enters_on_rebuild(self):
        self.office()
        self.label(QUIET, QUIET + 40, "present")
        self.record(5, 5, count=QUIET)
        self.record(5, 70, start=QUIET)
        assert self.human(self.b) == set()  # Attributed Present never enters live.
        assert self.inferred(self.b) == set()
        self.label(0, QUIET, "not_present")  # The room label edit marks every member stale.
        assert self.human(self.b) == set()
        await self.refresh()
        assert self.human(self.b) == {"not_present"}
        assert self.inferred(self.b) == {"present"}  # Rule 2 enters as "radar", not human.
        periods = self.b["reference_profile"]["periods"]
        present = [p for p in periods if p["state"] == "present"]
        assert sum(p["count"] for p in present) == 40
        assert min(p["start"] for p in present) == self.base + QUIET * 6

    def test_collect_samples_uses_own_label_first_then_attributed_room(self):
        self.office()
        self.record(5, 5, count=QUIET)
        self.record(5, 70, start=QUIET)
        self.label(0, QUIET, "not_present")
        self.label(QUIET, QUIET + 40, "present")
        start = self.base + QUIET * 6
        self.runtime.label_history_range("b", start, start + 60, "unknown")
        rows, guesses = evidence.collect_samples(self.runtime, "b", ["g0_move"])
        assert Counter(row[2] for row in rows) == {"not_present": QUIET}
        # Rule-2 attributed room Present is a weighted guess, outside the own Unknown label.
        assert Counter((g[2], g[3]) for g in guesses) == {("present", 0.6): 30}
        view = recording._history_view(self.runtime, "b")
        assert evidence.collect_samples(view, "b", ["g0_move"]) == (rows, guesses)

    def test_single_radar_groups_do_not_use_room_targets(self):
        self.office()
        groups.configure(self.runtime, "area:office", "Office", "office", ["b"])
        self.record(5, 70)
        self.label(0, 40, "present")
        assert attribution.room_targets(self.runtime, "b") is None
        assert recording._history_view(self.runtime, "b").data["room_snapshot"] is None
        rows, _ = evidence.collect_samples(self.runtime, "b", ["g0_move"])
        assert rows == []

    async def present_b(self, group="area:office"):
        """Record a quiet a and a high b, then label the room so b learns room Present
        (inferred by rule 2) and room Empty (human) at the next refresh."""
        self.record(5, 5, count=QUIET)
        self.record(5, 70, start=QUIET)
        self.label(0, QUIET, "not_present", group)
        self.label(QUIET, QUIET + 40, "present", group)
        await self.refresh()
        assert self.human(self.b) == {"not_present"}
        assert self.inferred(self.b) == {"present"}

    async def test_room_label_edit_during_single_device_learn_is_detected(self):
        self.office()
        del self.runtime._history_view  # The real view, so the fit reads room evidence.

        async def executor(fn, *args):
            self.label(0, 40, "not_present")  # A room edit lands while the fit runs.
            return {}

        self.hass.async_add_executor_job = executor
        with (
            patch.object(service, "read_timing", return_value={}),
            patch.object(service.recovery, "prepare_learning", AsyncMock(return_value=({}, {}))),
            self.assertRaises(service.LearningEvidenceChanged),
        ):
            await service._learn_once(self.runtime, "b")

    def test_room_label_edit_refuses_stale_learning_at_apply(self):
        self.office()
        learned = {
            "method": service.METHOD,
            "entities": {"g0_move": "number.b"},
            "proposals": {"g0_move": {"threshold": 20}},
            "label_revision": self.b.get("label_revision", 0),
        }
        self.b["last_learning"] = learned
        service._validate_learning(self.b, learned)
        self.label(0, 40, "not_present")
        assert "last_learning" not in self.b
        with self.assertRaisesRegex(ValueError, "Training labels changed"):
            service._validate_learning(self.b, learned)

    async def test_leaving_a_zone_clears_room_present_and_marks_learning_stale(self):
        self.office()
        groups.configure(self.runtime, "area:office", "Office", "office", ["a"])
        desk = groups.configure(self.runtime, None, "Desk", None, ["a", "b"])["id"]
        await self.present_b(desk)
        revision = self.b["label_revision"]
        groups.configure(self.runtime, desk, "Desk", None, ["a"])
        await self.refresh()
        assert self.human(self.b) == self.inferred(self.b) == set()
        assert self.b["label_revision"] == revision + 1
        groups.configure(self.runtime, desk, "Desk", None, ["a", "b"])
        await self.refresh()
        assert self.human(self.b) == {"not_present"}
        assert self.inferred(self.b) == {"present"}
        groups.remove(self.runtime, desk)
        await self.refresh()
        assert self.human(self.b) == self.inferred(self.b) == set()
        assert self.b["label_revision"] == revision + 3

    async def test_pausing_recording_leaves_the_area_and_clears_room_present(self):
        self.office()
        await self.present_b()
        revision = self.b["label_revision"]
        self.runtime.set_recording("b", False)
        await self.refresh()
        assert self.human(self.b) == self.inferred(self.b) == set()
        # One bump from ending live training (unchanged) and one from leaving the room.
        assert self.b["label_revision"] == revision + 2

    async def test_registry_update_that_drops_members_clears_room_present(self):
        self.office()
        await self.present_b()
        revision = self.b["label_revision"]
        self.runtime.handle_registry_update(None)  # No LD2410 entities remain registered.
        await self.refresh()
        assert self.human(self.b) == self.inferred(self.b) == set()
        assert self.b["label_revision"] == revision + 1

    def test_ungrouped_radar_live_observe_never_walks_the_registry(self):
        self.office()
        groups.configure(self.runtime, "area:office", "Office", "office", ["b"])
        self.label(0, 100, "not_present")
        with patch.object(groups, "definitions", wraps=groups.definitions) as walk:
            self.record(5, 5)
        assert walk.call_count == 0
        assert self.human(self.device) == set()

    async def test_room_targets_answer_between_recorded_samples(self):
        self.office()
        await self.present_b()
        target = attribution.room_targets(self.runtime, "b")
        assert target(self.base + QUIET * 6 + 3) == ("present", "attributed", 0.6)
        assert target(self.base + 3) == ("not_present", "human", 1.0)

    def member_labelled_office(self, b_present=None, b=70, quiet=QUIET):
        """a and b labelled Empty for ``quiet`` samples, then a labelled Present for 40 while b
        reads ``b``; ``b_present`` optionally labels b's own Present over that many samples of
        the episode. Room truth is human (derived from members); b's baseline is its own
        ``quiet`` Empty samples, so rule 2 attributes it at confidence ``quiet / 100``."""
        self.office()
        self.record(5, 5, count=quiet)
        self.record(5, b, start=quiet)
        self.episode = self.base + quiet * 6
        for key in ("a", "b"):
            self.runtime.label_history_range(key, self.base, self.episode, "not_present")
        self.runtime.label_history_range("a", self.episode, self.episode + 240, "present")
        if b_present:
            end = self.episode + b_present * 6
            self.runtime.label_history_range("b", self.episode, end, "present")

    def test_rule_two_attribution_trains_as_weighted_guess_never_human(self):
        # Regression (real data): an unlabelled member whose energy rises above its own empty
        # baseline while another member is labelled Present was trained as human Present.
        self.member_labelled_office()
        rows, guesses = evidence.collect_samples(self.runtime, "b", ["g0_move"])
        assert Counter(row[2] for row in rows) == {"not_present": QUIET}
        assert Counter((g[2], g[3]) for g in guesses) == {("present", 0.6): 40}
        # The learner itself keeps them as automatic evidence: the weight setting x confidence 0.6.
        fitted = fitting.fit_thresholds(rows, ["g0_move"], {"g0_move": 50}, guesses)
        automatic = fitted["automatic_evidence"]
        assert automatic["samples"] == {"present": 40, "not_present": 0}
        assert automatic["mean_confidence"]["present"] == 0.6
        assert automatic["base_weight"] == settings.DEFAULT_AUTOMATIC_WEIGHT
        assert fitted["counts"] == {"present": 0, "not_present": QUIET}
        assert attribution.room_targets(self.runtime, "b")(self.episode + 3) == (
            "present",
            "attributed",
            0.6,
        )

    def test_rule_two_below_the_automatic_floor_gives_no_target(self):
        # 40 quiet samples give confidence 0.4 < MIN_AUTO_CONFIDENCE, like a floored estimate.
        self.member_labelled_office(quiet=40)
        rows, guesses = evidence.collect_samples(self.runtime, "b", ["g0_move"])
        assert Counter(row[2] for row in rows) == {"not_present": 40}
        assert guesses == []
        assert attribution.room_targets(self.runtime, "b")(self.episode + 3) is None
        current = {"id": "x", "thresholds": {"g0_move": 50}, "applicable": True}
        context = {"keys": ["g0_move"], "timing": {"mode": "disabled"}, "patterns": {"c": current}}
        assert comparison.calculate(self.runtime, "b", context)["counts"]["automatic"] == {
            "present": 0,
            "not_present": 0,
        }
        training.rebuild_human(self.runtime, self.b)
        periods = self.b["reference_profile"]["periods"]
        assert not [p for p in periods if p["state"] == "present"]

    def test_rule_two_rows_are_not_scoring_ground_truth(self):
        self.member_labelled_office()
        current = {"id": "x", "thresholds": {"g0_move": 50}, "applicable": True}
        context = {"keys": ["g0_move"], "timing": {"mode": "disabled"}}
        result = comparison.calculate(self.runtime, "b", {**context, "patterns": {"c": current}})
        assert result["counts"]["human"] == {"present": 0, "not_present": QUIET}
        assert result["counts"]["automatic"]["present"] == 40

    def test_rule_one_attribution_stays_human_at_full_weight(self):
        # b's own Present label inside the episode attributes it (rule 1), even while quiet.
        self.member_labelled_office(b_present=5, b=5)
        rows, guesses = evidence.collect_samples(self.runtime, "b", ["g0_move"])
        assert Counter(row[2] for row in rows) == {"not_present": QUIET, "present": 40}
        assert guesses == []
        assert attribution.room_targets(self.runtime, "b")(self.episode + 60) == (
            "present",
            "human",
            1.0,
        )

    def test_human_room_empty_is_a_human_quiet_target_for_every_member(self):
        self.office()
        self.record(5, 5)
        self.label(0, 40, "not_present")
        for key in ("a", "b"):
            rows, guesses = evidence.collect_samples(self.runtime, key, ["g0_move"])
            assert Counter(row[2] for row in rows) == {"not_present": 40}
            assert guesses == []

    def test_rule_two_present_enters_references_as_radar_never_human(self):
        self.member_labelled_office()
        training.rebuild_human(self.runtime, self.b)
        assert self.human(self.b) == {"not_present"}
        periods = self.b["reference_profile"]["periods"]
        present = [p for p in periods if p["state"] == "present"]
        assert {(p["source"], p["confidence"], bool(p.get("room"))) for p in present} == {
            ("radar", 0.6, True)
        }
        assert sum(p["count"] for p in present) == 40

    def test_a_rebuild_reads_the_history_once_for_all_its_evidence(self):
        """Human labels and entity and radar room evidence come from one pass over the samples,
        not one each."""
        self.member_labelled_office()
        reader = self.runtime._iter_history_samples
        with patch.object(self.runtime, "_iter_history_samples", wraps=reader) as read:
            training.rebuild_human(self.runtime, self.b)
        # The attribution's rows come as columns; the one pass over the samples is this.
        assert read.call_count == 1

    def test_live_room_empty_reads_only_the_radars_own_groups_and_expires_its_cache(self):
        self.office()
        shelf = groups.configure(self.runtime, None, "Shelf", None, ["a"])["id"]
        self.label(0, 100, "not_present", shelf)  # Not one of b's groups.
        self.runtime._room_membership["at"] -= attribution.MEMBERSHIP_TTL + 1
        with patch.object(groups, "definitions", wraps=groups.definitions) as walk:
            self.record(5, 5)
        assert walk.call_count == 1  # One refresh after expiry, shared by every later sample.
        assert self.human(self.b) == set()


class AutomaticRoomTruthTests(unittest.IsolatedAsyncioTestCase):
    """Level-4 room truth (sensor labels, estimate) trains radars at reduced weight."""

    setUp = harness.RuntimeTests.setUp
    asyncTearDown = harness.RuntimeTests.asyncTearDown
    office = RoomReferenceTests.office
    record = RoomReferenceTests.record
    label = RoomReferenceTests.label

    def record_guessed(self, a, b, guesses, start=0, count=40):
        """Record a and b readings while each radar's own classifier says ``guesses``."""
        for i in range(start, start + count):
            when = self.base + i * 6
            for key, value in (("a", a), ("b", b)):
                state, confidence = guesses[key]
                device = self.runtime.data["devices"][key]
                device.setdefault("auto", {})["last_classification"] = {
                    "state": state,
                    "timestamp": when,
                    "confidence": confidence,
                }
                self.runtime._record_history_sample(key, {"g0_move": value}, when)

    def scenario(self):
        """a and b both guess; b is quiet then high. The estimate counts every member's guess at
        its calibrated confidence (whatever the recorded one, 0.95 for not_present and for a
        present guess of a fresh or no reference): quiet is Empty at 0.95, high is Present at
        noisy-OR(0.95, 0.95) = 0.9975."""
        self.office()
        self.record_guessed(5, 5, {"a": ("not_present", 0.6), "b": ("not_present", 0.9)})
        self.record_guessed(5, 70, {"a": ("present", 1.0), "b": ("present", 0.65)}, start=40)

    def room_guesses(self):
        return list(evidence.collect_samples(self.runtime, "b", ["g0_move"])[1])

    def test_estimate_trains_the_radar_with_confidence_weighted_rows(self):
        self.scenario()
        guesses = self.room_guesses()
        assert Counter((g[2], round(g[3], 4)) for g in guesses) == {
            ("not_present", 0.95): 40,
            ("present", 0.9975): 39,
            ("present", 0.95): 1,  # The last sample has no estimate cell: only b's own guess.
        }
        # Their confidence mass, not their count, sets the learner's automatic share.
        confident = [g[3] for g in guesses if g[3] > 0.99]
        assert scoring.automatic_share(1.0, 100, sum(confident)) == pytest.approx(
            0.25 * 39 * 0.9975 / 100
        )

    def test_history_view_carries_other_members_guesses(self):
        self.scenario()
        view = recording._history_view(self.runtime, "b")
        assert evidence.collect_samples(view, "b", ["g0_move"])[1] == self.room_guesses()

    def test_own_guess_contributes_to_its_own_estimate(self):
        self.office()
        empty = {"a": ("not_present", 0.9), "b": ("not_present", 0.9)}
        self.record_guessed(5, 5, empty)
        self.record_guessed(5, 70, {"a": ("present", 1.0), "b": ("present", 1.0)}, start=40)
        # a is confidently Empty again, so b's own guess alone does not make the room Present.
        self.record_guessed(5, 70, {"a": empty["a"], "b": ("present", 1.0)}, start=80)
        target = attribution.auto_targets(self.runtime, "b")
        assert target(self.base + 3) == ("not_present", "estimate", 0.95)
        assert target(self.base + 243) == ("present", "estimate", pytest.approx(0.9975))
        # b's own guess counts towards the agreement (it is one of the two) ...
        assert target(self.base + 483) is None
        # ... but a present guess of its own is not a room's.

    def test_human_room_empty_and_rule_one_present_override_automatic_at_full_weight(self):
        self.scenario()
        self.label(0, 40, "not_present")
        self.label(40, 80, "present")
        # b's own Present label in the episode (rule 1) makes room Present human for b.
        self.runtime.label_history_range("b", self.base + 240, self.base + 246, "present")
        rows, guesses = evidence.collect_samples(self.runtime, "b", ["g0_move"])
        assert Counter(row[2] for row in rows) == {"not_present": 40, "present": 40}
        assert guesses == []  # Human truth covers every sample: nothing automatic is left.
        target = attribution.auto_targets(self.runtime, "b")
        assert target(self.base + 3) == ("not_present", "human", 1.0)
        assert target(self.base + 303) == ("present", "human", 1.0)

    def inferred_room(self, present):
        """b's baseline is 60 human-Empty samples (rule 2 at 0.6); then the room is human
        Present for 40 while b rises unlabelled and the members guess ``present``."""
        self.office()
        quiet = {"a": ("not_present", 0.6), "b": ("not_present", 0.9)}
        self.record_guessed(5, 5, quiet, count=QUIET)
        self.record_guessed(5, 70, present, start=QUIET)
        self.label(0, QUIET, "not_present")
        self.label(QUIET, QUIET + 40, "present")
        return self.base + QUIET * 6 + 3

    def room_present_periods(self):
        training.rebuild_human(self.runtime, self.b)
        periods = self.b["reference_profile"]["periods"]
        return {
            (p["source"], round(p["confidence"], 2)) for p in periods if p["state"] == "present"
        }

    def test_rule_two_guess_yields_to_a_more_confident_automatic_estimate(self):
        # The estimate is noisy-OR(0.95, 0.95) = 0.9975 > the 0.6 attribution guess.
        t = self.inferred_room({"a": ("present", 1.0), "b": ("present", 0.65)})
        assert attribution.auto_targets(self.runtime, "b")(t) == (
            "present",
            "estimate",
            pytest.approx(0.9975),
        )
        rows, guesses = evidence.collect_samples(self.runtime, "b", ["g0_move"])
        assert Counter(row[2] for row in rows) == {"not_present": QUIET}
        # The last sample has no estimate cell, so the attribution guess decides it.
        assert Counter((g[2], round(g[3], 4)) for g in guesses) == {
            ("present", 0.9975): 39,
            ("present", 0.6): 1,
        }
        # One "radar" period, at the mean confidence (39 x 0.9975 + 0.6) / 40, never human.
        assert self.room_present_periods() == {("radar", 0.99)}

    def test_rule_two_guess_beats_a_less_confident_estimate_and_yields_to_a_sensor(self):
        # Present guesses of a stale reference count for nothing: no estimate, only the 0.6
        # attribution guess.
        t = self.inferred_room({"a": ("present", 0.9), "b": ("present", 0.8)})
        assert attribution.auto_targets(self.runtime, "b")(t) == ("present", "attributed", 0.6)
        guesses = evidence.collect_samples(self.runtime, "b", ["g0_move"])[1]
        assert Counter((g[2], round(g[3], 2)) for g in guesses) == {("present", 0.6): 40}
        assert self.room_present_periods() == {("radar", 0.6)}
        # A more confident room sensor label of Present wins, and enters as "entity".
        self.runtime.data["room_labels"]["area:office"].append(
            {
                "start": self.base + QUIET * 6,
                "end": self.base + (QUIET + 40) * 6,
                "state": "present",
                "source": "sensor",
                "confidence": 0.9,
            }
        )
        assert attribution.auto_targets(self.runtime, "b")(t) == ("present", "automatic", 0.9)
        guesses = evidence.collect_samples(self.runtime, "b", ["g0_move"])[1]
        assert Counter((g[2], round(g[3], 2)) for g in guesses) == {("present", 0.9): 40}
        assert self.room_present_periods() == {("entity", 0.9)}

    def test_references_take_estimate_as_radar_and_sensor_as_entity_never_human(self):
        self.scenario()
        training.rebuild_human(self.runtime, self.b)
        periods = [p for p in self.b["reference_profile"]["periods"] if p.get("room")]
        assert {p["source"] for p in periods} == {"radar"}
        assert {(p["state"], round(p["confidence"], 2)) for p in periods} == {
            ("not_present", 0.95),
            ("present", 1.0),
        }
        self.runtime.data.setdefault("room_labels", {})["area:office"] = [
            {
                "start": self.base,
                "end": self.base + 240,
                "state": "not_present",
                "source": "sensor",
                "confidence": 0.7,
            }
        ]
        training.rebuild_human(self.runtime, self.b)
        periods = [p for p in self.b["reference_profile"]["periods"] if p.get("room")]
        entity = [p for p in periods if p["source"] == "entity"]
        assert {(p["state"], round(p["confidence"], 2)) for p in entity} == {("not_present", 0.7)}
        assert not [p for p in periods if p["source"] == "human"]
        assert {p["source"] for p in periods if p["state"] == "present"} == {"radar"}

    def test_no_guesses_or_sensors_changes_nothing(self):
        self.office()
        self.record(5, 5)
        self.record(5, 70, start=40)
        assert attribution.auto_targets(self.runtime, "b") is None
        assert evidence.collect_samples(self.runtime, "b", ["g0_move"])[1] == []
        training.rebuild_human(self.runtime, self.b)
        assert not [p for p in self.b["reference_profile"]["periods"] if p.get("room")]

    def test_room_automatic_evidence_outranks_own_guess_but_own_human_label_wins(self):
        self.scenario()
        self.runtime.label_history_range("b", self.base, self.base + 60, "present")
        self.runtime.data.setdefault("room_labels", {})["area:office"] = [
            {
                "start": self.base,
                "end": self.base + 240,
                "state": "present",
                "source": "sensor",
                "confidence": 0.7,
            }
        ]
        rows, guesses = evidence.collect_samples(self.runtime, "b", ["g0_move"])
        assert sum(1 for row in rows if row[2] == "present") == 10  # Own human label first.
        early = [g for g in guesses if g[0] >= self.base + 60 and g[0] < self.base + 240]
        # The room sensor (Present, 0.7), not b's own guess (Empty, 0.95), decides each sample.
        assert {(g[2], round(g[3], 2)) for g in early} == {("present", 0.7)}

    def test_sensor_stretches_are_entity_only_and_estimate_stretches_radar_only(self):
        self.scenario()
        self.runtime.data.setdefault("room_labels", {})["area:office"] = [
            {
                "start": self.base,
                "end": self.base + 240,
                "state": "not_present",
                "source": "sensor",
                "confidence": 0.7,
            }
        ]
        training.rebuild_human(self.runtime, self.b)
        room = [p for p in self.b["reference_profile"]["periods"] if p.get("room")]
        assert {(p["source"], p["state"]) for p in room} == {
            ("entity", "not_present"),
            ("radar", "present"),
        }
        assert sum(p["count"] for p in room if p["source"] == "entity") == 40

    async def test_refresh_decodes_each_members_guess_chunk_once(self):
        """Every member's guess runs are read from their stored chunks once per chunk, however
        many views and attribution passes read them."""
        from tuner_under_test.presence import rebuilds

        guesses = sys.modules["tuner_under_test.history.guesses"]
        self.scenario()
        self.runtime.data["devices"]["c"] = deepcopy(self.runtime.data["devices"]["a"])
        groups.configure(self.runtime, "area:office", "Office", "office", ["a", "b", "c"])
        for device_id in "abc":
            self.runtime._flush_history_block(device_id)
        chunks = {
            c["data"] for key in "abc" for c in self.runtime.data["devices"][key]["guess_runs"]
        }
        guesses._decoded.cache_clear()
        await rebuilds.refresh(self.runtime, {"a", "b", "c"}, reason="test")
        assert 0 < guesses._decoded.cache_info().misses <= len(chunks)

    def test_a_finished_job_lets_go_of_the_members_guesses_its_views_shared(self):
        shared = {"a": object()}
        views = [types.SimpleNamespace(_attribution_guesses=shared, _attribution_both={})] * 2
        attribution.release(views)
        assert shared == {}

    async def test_the_views_of_a_refresh_build_each_members_guesses_once(self):
        """Regression: every view built every member's guesses over the full history, so a
        refresh of n members built them n times n; the views of a job share one set."""
        from tuner_under_test.presence import rebuilds

        guesses = sys.modules["tuner_under_test.history.guesses"]
        self.scenario()
        self.runtime.data["devices"]["c"] = deepcopy(self.runtime.data["devices"]["a"])
        groups.configure(self.runtime, "area:office", "Office", "office", ["a", "b", "c"])
        for device_id in "abc":
            self.runtime._flush_history_block(device_id)
        with patch.object(guesses, "of", wraps=guesses.of) as read:
            await rebuilds.refresh(self.runtime, {"a", "b", "c"}, reason="test")
        assert read.call_count == 3
