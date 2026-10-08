"""Persistent, correctable reference learning under changing room conditions."""

import json
import sys
import unittest
from copy import deepcopy
from unittest.mock import patch

import test_sources as source_harness
import test_tuner as harness

refs = sys.modules["tuner_under_test.presence.references"]
training = sys.modules["tuner_under_test.presence.reference_training"]
inference = sys.modules["tuner_under_test.presence.inference"]
cleanup = sys.modules["tuner_under_test.history.cleanup"]
budget = sys.modules["tuner_under_test.history.budget"]
NOW = 2_000_000_000


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

    def test_retroactive_correction_replaces_human_and_removes_conflicting_entity_summary(self):
        self.configure(negative=True)
        self.entity("binary_sensor.camera_person", "off")
        for i in range(30):
            self.sample(self.now + i * 6, 70)
        self.runtime.label_history_range("a", self.now, self.now + 180, "present")
        periods = self.device["reference_profile"]["periods"]
        assert {p["source"] for p in periods} == {"human"}
        assert {p["state"] for p in periods} == {"present"}
        self.runtime.label_history_range("a", self.now, self.now + 180, "unknown")
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
