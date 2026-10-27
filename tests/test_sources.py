"""Independent source labels retain manual priority and automatic retention."""

import sys
import time
import types
import unittest
from copy import deepcopy
from unittest.mock import patch

import pytest
import test_tuner as harness

sources = sys.modules["tuner_under_test.presence.sources"]
service = sys.modules["tuner_under_test.calibration.service"]
event_fitting = sys.modules["tuner_under_test.calibration.event_fitting"]
policy = sys.modules["tuner_under_test.history.policy"]


def boolean(entity_id="binary_sensor.camera_person"):
    return {"entity_id": entity_id, "kind": "boolean", "area": ""}


def bermuda():
    return {"entity_id": "sensor.phone_area", "kind": "bermuda", "area": "bedroom"}


class SourceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        harness.RuntimeTests.setUp(self)
        # Day files quantize timestamps to milliseconds; duration assertions use
        # an integral synthetic origin instead of the harness's fractional wall time.
        self.now = 2_000_000_000.0

    asyncTearDown = harness.RuntimeTests.asyncTearDown
    sample = harness.RuntimeTests.sample

    def entity(self, entity_id, state, **attributes):
        self.states[entity_id] = types.SimpleNamespace(state=state, attributes=attributes)

    def configure(self, entries=None, negative=False):
        entries = entries or [boolean()]
        return self.runtime.configure_presence_sources(
            "a",
            {
                "sources": entries,
                "mark_not_present": negative,
                "confidence": 90,
                "start_buffer_seconds": 0,
                "end_buffer_seconds": 0,
            },
        )

    def test_boolean_on_records_automatic_not_human_and_survives_a_save(self):
        self.configure()
        self.entity("binary_sensor.camera_person", "on")
        self.sample(self.now, 70)
        (run,) = harness.guesses.of(self.runtime, "a").runs
        assert run[2:] == ("present", 18, harness.guesses.EXTERNAL)  # 90 % is 18 steps of 5 %
        assert not self.device.get("history_labels")
        assert sum(self.device.get("histograms", {}).get("g0_move", {}).get("present", [])) == 0
        assert self.device["auto"]["last_classification"]["basis"] == "external"
        self.runtime._days.flush()
        (saved,) = harness.guesses.of(self.runtime, "a").runs  # Still open: it ends now.
        assert (saved[0], *saved[2:]) == (run[0], *run[2:])
        with (
            patch.object(time, "time", return_value=self.now + 1),
            self.assertRaisesRegex(ValueError, "Correct external labels"),
        ):
            self.runtime.record_auto_feedback("a", False)

    def test_off_is_opt_in_and_unknown_never_means_absence(self):
        self.entity("binary_sensor.camera_person", "off")
        config = self.configure()
        assert config["mark_not_present"] is False
        assert sources.estimate(self.runtime, self.device) is None
        self.configure(negative=True)
        assert sources.estimate(self.runtime, self.device)["state"] == "not_present"
        for value in ("unknown", "unavailable", "None", "unexpected", ""):
            self.entity("binary_sensor.camera_person", value)
            assert sources.estimate(self.runtime, self.device) is None
        self.states.pop("binary_sensor.camera_person")
        assert sources.estimate(self.runtime, self.device) is None

    def test_any_boolean_domain_and_true_false_are_supported(self):
        self.configure([boolean("input_boolean.room_occupied")], negative=True)
        for value, expected in (("true", "present"), ("false", "not_present"), ("on", "present")):
            self.entity("input_boolean.room_occupied", value)
            assert sources.estimate(self.runtime, self.device)["state"] == expected

    def test_bermuda_matches_current_area_id_or_name_and_mixed_sources_use_or(self):
        config = {**sources.DEFAULTS, "sources": [boolean(), bermuda()], "mark_not_present": True}
        self.entity("binary_sensor.camera_person", "off")
        self.entity(
            "sensor.phone_area", "Main Bedroom", area_id="bedroom", area_name="Main Bedroom"
        )
        assert sources.read(self.hass, config)["state"] == "present"
        self.entity("sensor.phone_area", "Office", area_id="office")
        assert sources.source_state(self.hass, bermuda()) is None
        assert sources.read(self.hass, config)["state"] == "not_present"
        self.entity("sensor.phone_area", "unavailable", area_id="bedroom")
        assert sources.read(self.hass, config)["state"] == "not_present"
        config["sources"].append(boolean("binary_sensor.other"))
        assert sources.read(self.hass, config)["state"] == "unknown"
        self.entity("binary_sensor.camera_person", "on")
        assert sources.read(self.hass, config)["state"] == "present"
        self.entity("sensor.phone_area", "Main Bedroom")
        assert sources.source_state(self.hass, {**bermuda(), "area": "Main Bedroom"}) is True

    async def test_manual_corrections_win_in_the_actual_learning_input(self):
        self.configure(negative=True)
        self.entity("binary_sensor.camera_person", "on")
        self.sample(self.now, 70)
        self.entity("binary_sensor.camera_person", "off")
        self.sample(self.now + 6, 10)

        def measured():
            with patch.object(time, "time", return_value=self.now + 12):
                found = event_fitting.assess(
                    self.runtime, "a", {"g0_move": 20}, quality_context=False
                )
            return {
                source: tuple(
                    round(found[source]["duration"][key], 3)
                    for key in ("present_seconds", "empty_seconds")
                )
                for source in ("human", "automatic")
            }

        await self.runtime.label_history_range("a", self.now - 1, self.now + 1, "not_present")
        # Human [0, 1) s outranks the external present guess there; the guesses keep the rest.
        assert measured() == {"human": (0, 1), "automatic": (5, 6)}
        await self.runtime.label_history_range("a", self.now + 5, self.now + 7, "unknown")
        assert measured() == {"human": (0, 1), "automatic": (4, 5)}  # Excluded: no evidence.

    def test_bermuda_positive_only_and_boolean_absence(self):
        source = {**bermuda(), "max_distance": 3}
        config = {**sources.DEFAULTS, "sources": [source], "mark_not_present": True}
        self.registry.entities.update(
            {
                "sensor.phone_area": types.SimpleNamespace(platform="bermuda", unique_id="phone"),
                "sensor.renamed_distance": types.SimpleNamespace(
                    platform="bermuda", unique_id="phone_range"
                ),
            }
        )
        self.entity("sensor.phone_area", "Bedroom", area_id="bedroom")
        for value, present, label, distance in (
            ("2.3", True, "present", 2.3),
            ("3", True, "present", 3.0),
            ("0", True, "present", 0.0),
            ("4.1", None, "unknown", 4.1),
            ("unknown", None, "unknown", None),
            ("unavailable", None, "unknown", None),
            ("invalid", None, "unknown", None),
            ("nan", None, "unknown", None),
            ("inf", None, "unknown", None),
        ):
            self.entity("sensor.renamed_distance", value)
            result = sources.read(self.hass, config)
            assert result["state"] == label
            assert result["readings"][0]["present"] is present
            assert result["readings"][0]["distance"] == distance
            assert sources.source_state(self.hass, source) is present
        # Outside the target area, or with no usable area, the distance decides nothing,
        # so the reading carries none (the panel must not call it "too far").
        self.entity("sensor.renamed_distance", "4.1")
        for area in ({"state": "Office", "area_id": "office"}, {"state": "unavailable"}):
            self.entity("sensor.phone_area", area.pop("state"), **area)
            (reading,) = sources.read(self.hass, config)["readings"]
            assert "distance" not in reading

        config["sources"].append(boolean())
        self.entity("binary_sensor.camera_person", "off")
        assert sources.read(self.hass, config)["state"] == "not_present"
        self.entity("binary_sensor.camera_person", "unavailable")
        assert sources.read(self.hass, config)["state"] == "unknown"
        self.entity("sensor.phone_area", "Bedroom", area_id="bedroom")
        self.entity("sensor.renamed_distance", "3")
        assert sources.read(self.hass, config)["state"] == "present"

    def test_bermuda_missing_distance_and_area_mismatch_give_no_information(self):
        source = {**bermuda(), "max_distance": 3}
        self.entity("sensor.phone_area", "Bedroom", area_id="bedroom")
        assert sources.source_state(self.hass, source) is None
        self.registry.entities["sensor.phone_area"] = types.SimpleNamespace(
            platform="other", unique_id="phone"
        )
        assert sources.source_state(self.hass, source) is None
        self.registry.entities["sensor.phone_area"].platform = "bermuda"
        assert sources.source_state(self.hass, source) is None
        self.registry.entities["sensor.phone_range"] = types.SimpleNamespace(
            platform="bermuda", unique_id="phone_range"
        )
        assert sources.source_state(self.hass, source) is None
        self.entity("sensor.phone_area", "Office", area_id="office")
        assert sources.source_state(self.hass, source) is None
        assert sources.source_state(self.hass, bermuda()) is None
        self.entity("sensor.phone_area", "unknown", area_id="bedroom")
        assert sources.source_state(self.hass, source) is None

    async def test_external_records_follow_automatic_expiry_and_recording_pause(self):
        from tuner_under_test.history import budget, ingest, spans

        self.configure()
        self.entity("binary_sensor.camera_person", "on")
        self.sample(self.now, 70)
        ingest.end(self.runtime, "a", self.now + 6)
        self.runtime._days.flush(self.now + 30 * policy.DAY)
        self.runtime.data["storage_settings"] = {**policy.DEFAULTS, "human_days": 60}
        _prepared, changes, stats = budget.prepare_days(
            self.runtime._days, self.runtime.data, self.now + 30 * policy.DAY, clean=True
        )
        assert stats["removed_automatic_seconds"] == 6
        assert stats["removed_human_seconds"] == 0
        for key, date, day in changes:
            self.runtime._days.replace(key, date, day)
        assert not list(spans.events(self.runtime, "a"))
        self.runtime.set_recording("a", False)
        before = deepcopy(self.device)
        self.sample(self.now + 6, 90)
        assert self.device == before

    def test_sources_alone_produce_a_threshold_fit_without_human_validation_claims(self):
        self.configure(negative=True)
        for index in range(120):
            self.entity("binary_sensor.camera_person", "on" if index < 60 else "off")
            self.sample(self.now + index * 6, 70 if index < 60 else 10)
        with patch.object(time, "time", return_value=self.now + 720):
            learned = service._fit_history(
                self.runtime, "a", {"g0_move": "number.threshold"}, {"g0_move": 20}
            )
        assert 10 < learned["proposals"]["g0_move"]["threshold"] < 70
        samples = learned["automatic_evidence"]["samples"]  # Six-second units of time.
        assert samples == {"present": pytest.approx(60), "not_present": pytest.approx(60)}
        assert learned["training"]["present_samples"] == 0

    async def test_settings_persist_and_clearing_sources_restores_estimator(self):
        self.configure()
        self.entity("binary_sensor.camera_person", "on")
        self.sample(self.now)
        await self.runtime.async_save()
        saved = self.runtime.store.async_save.call_args.args[0]
        assert saved["devices"]["a"]["presence_sources"] == sources.settings(self.device)
        self.runtime.configure_presence_sources("a", deepcopy(sources.DEFAULTS))
        assert sources.estimate(self.runtime, self.device) is None
        with patch.object(
            self.runtime, "_classify_auto", wraps=self.runtime._classify_auto
        ) as classify:
            self.sample(self.now + 6)
        classify.assert_called_once()
        assert self.device["auto"]["last_classification"]["basis"] != "external"
        with self.assertRaisesRegex(ValueError, "Unknown device"):
            self.runtime.configure_presence_sources("missing", deepcopy(sources.DEFAULTS))


@pytest.mark.parametrize(
    "change",
    [
        {"mark_not_present": 1},
        {"confidence": float("nan")},
        {"confidence": 101},
        {"confidence": True},
        {"sources": None},
        {"sources": [boolean()] * 17},
        {"sources": ["bad"]},
        {"sources": [{**boolean(), "kind": "camera"}]},
        {"sources": [{**boolean(), "entity_id": "not an id"}]},
        {"sources": [{**bermuda(), "area": ""}]},
        {"sources": [{**bermuda(), "area": None}]},
        {"sources": [{"kind": "boolean"}]},
        {"extra": True},
    ],
)
def test_invalid_settings_are_rejected(change):
    with pytest.raises(ValueError):
        sources.validate({**sources.DEFAULTS, **change})


@pytest.mark.parametrize("distance", [0.1, 1, 3.5, 100])
def test_bermuda_max_distance_is_preserved(distance):
    source = {**bermuda(), "max_distance": distance}
    sources._validate_source(source)
    assert source["max_distance"] == distance


@pytest.mark.parametrize("distance", [0, -1, 100.1, float("nan"), float("inf"), True, "3", None])
def test_invalid_bermuda_max_distance_is_rejected(distance):
    with pytest.raises(ValueError):
        sources._validate_source({**bermuda(), "max_distance": distance})


def test_boolean_max_distance_is_rejected():
    with pytest.raises(ValueError):
        sources.validate({**sources.DEFAULTS, "sources": [{**boolean(), "max_distance": 3}]})
