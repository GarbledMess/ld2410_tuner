"""Central Bermuda assignments use synthetic registries and states only."""

import sys
import types
import unittest
from copy import deepcopy
from unittest.mock import Mock, patch

import pytest
import test_sources as source_harness
import test_tuner as harness
from test_sources import bermuda, boolean, sources


class BermudaTests(unittest.IsolatedAsyncioTestCase):
    setUp = harness.RuntimeTests.setUp
    asyncTearDown = harness.RuntimeTests.asyncTearDown
    entity = source_harness.SourceTests.entity

    def areas(self):
        self.devreg = types.SimpleNamespace(
            async_get=lambda key: {
                "a": types.SimpleNamespace(area_id="bedroom", name="Radar A", name_by_user=None),
                "b": types.SimpleNamespace(area_id="office", name="Radar B", name_by_user=None),
            }.get(key)
        )
        dr = sys.modules["homeassistant.helpers.device_registry"]
        self.enterContext(patch.object(dr, "async_get", return_value=self.devreg))
        ar = sys.modules["homeassistant.helpers.area_registry"]
        areas = types.SimpleNamespace(
            async_list_areas=lambda: [
                types.SimpleNamespace(id="bedroom", name="Main Bedroom"),
                types.SimpleNamespace(id="office", name="Office"),
            ]
        )
        self.enterContext(patch.object(ar, "async_get", return_value=areas))
        self.runtime.data["devices"].update({"b": {}, "c": {}})

    def inventory(self):
        for entity_id, platform, unique_id in (
            ("sensor.phone_area", "bermuda", "phone"),
            ("sensor.renamed_distance", "bermuda", "phone_range"),
            ("sensor.no_sibling", "bermuda", "alone"),
            ("sensor.other", "other", "other"),
            ("sensor.other_range", "bermuda", "other_range"),
            ("binary_sensor.watch", "bermuda", "watch"),
            ("sensor.watch_range", "bermuda", "watch_range"),
        ):
            self.registry.entities[entity_id] = types.SimpleNamespace(
                entity_id=entity_id,
                platform=platform,
                unique_id=unique_id,
                domain=entity_id.split(".")[0],
                device_id=None,
                name=None,
                original_name="Phone <watch>",
            )
        self.entity(
            "sensor.phone_area",
            "Main Bedroom",
            area_id="bedroom",
            area_name="Main Bedroom",
            friendly_name="Jack’s phone",
        )

    def config(self, **radars):
        return {"devices": {"sensor.phone_area": {"radars": radars}}}

    def test_discovery_requires_sensor_platform_and_range_sibling(self):
        self.inventory()
        assert self.runtime.bermuda_devices() == [
            {
                "entity_id": "sensor.phone_area",
                "name": "Jack’s phone",
                "area": "Main Bedroom",
                "area_id": "bedroom",
            }
        ]
        self.states.clear()
        assert self.runtime.bermuda_devices()[0]["name"] == "Phone <watch>"
        assert self.runtime.bermuda_devices()[0]["area"] is None

    def test_settings_validate_copy_and_snapshot(self):
        self.areas()
        self.inventory()
        values = self.config(a={"max_distance": 3}, c={"max_distance": None})
        self.runtime._schedule_save = Mock()
        assert self.runtime.configure_bermuda(values) == values
        values["devices"].clear()
        self.runtime._schedule_save.assert_called_once()
        # Refresh normally discovers gate entities; retain this synthetic radar for snapshot.
        self.runtime.refresh_devices = lambda registry: None
        snapshot = self.runtime.snapshot()
        assert snapshot["bermuda"] == self.config(a={"max_distance": 3}, c={"max_distance": None})
        assert snapshot["bermuda_radars"]["a"] == {"area_id": "bedroom", "reason": None}
        assert "Home Assistant area" in snapshot["bermuda_radars"]["c"]["reason"]
        assert snapshot["bermuda_devices"] == self.runtime.bermuda_devices()
        snapshot["bermuda"]["devices"].clear()
        assert self.runtime.data["bermuda"]["devices"]

    def test_invalid_settings_are_atomic(self):
        good = self.config(a={"max_distance": None})
        self.runtime.configure_bermuda(good)
        bad = [
            None,
            [],
            {},
            {"devices": []},
            {"devices": {}, "extra": 1},
            {"devices": {"bad": {"radars": {}}}},
            {"devices": {"sensor.phone_area": {"radars": []}}},
            {"devices": {"sensor.phone_area": {"radars": {}, "extra": 1}}},
            self.config(missing={"max_distance": 3}),
            self.config(a={}),
            self.config(a={"max_distance": None, "extra": 1}),
        ]
        bad += [
            self.config(a={"max_distance": v})
            for v in (0, -1, 101, True, "3", float("nan"), float("inf"))
        ]
        for value in bad:
            with pytest.raises(ValueError):
                self.runtime.configure_bermuda(value)
            assert self.runtime.data["bermuda"] == good
        for value in (None, 0.01, 100):
            assert self.runtime.configure_bermuda(self.config(a={"max_distance": value}))

    def test_effective_sources_use_each_radars_area_and_local_policy(self):
        self.areas()
        self.inventory()
        self.device["presence_sources"] = {
            **sources.DEFAULTS,
            "sources": [boolean()],
            "confidence": 85,
            "mark_not_present": True,
            "start_buffer_seconds": 0,
            "end_buffer_seconds": 0,
        }
        values = self.config(a={"max_distance": 3}, b={"max_distance": None}, c={"max_distance": 2})
        self.runtime.configure_bermuda(values)
        self.entity("sensor.renamed_distance", "2.5")
        self.entity("binary_sensor.camera_person", "off")
        evidence = sources._read_sources(self.runtime, self.device)
        assert evidence["state"] == "present"
        assert evidence["confidence"] == 85
        assert evidence["readings"][1] == {
            **bermuda(),
            "max_distance": 3,
            "present": True,
            "distance": 2.5,
        }
        assert sources.estimate(self.runtime, self.device)["confidence"] == 0.85
        self.entity("sensor.renamed_distance", "4")
        assert sources._read_sources(self.runtime, self.device)["state"] == "not_present"
        assert sources._read_sources(self.runtime, self.runtime.data["devices"]["b"])[
            "sources"
        ] == [{**bermuda(), "area": "office"}]
        assert (
            sources._read_sources(self.runtime, self.runtime.data["devices"]["c"])["sources"] == []
        )
        assert sources.summary(self.runtime, self.device)["sources"] == [boolean()], (
            "central assignments must not leak into the legacy editor"
        )

    def test_changed_settings_reset_only_affected_radars(self):
        self.areas()
        self.runtime.configure_bermuda(self.config(a={"max_distance": 2}))
        for key, device in self.runtime.data["devices"].items():
            self.runtime._source_runtime[key] = {"pending": True}
            self.runtime._auto_runtime[key] = {"stale": True}
            device["auto"] = {"last_classification": {"state": "present"}}
        with patch.object(sources.reference_training, "reset_entities") as reset:
            self.runtime.configure_bermuda(self.config(a={"max_distance": 3}))
            reset.assert_called_once_with(self.device)
        assert "a" not in self.runtime._source_runtime
        assert "a" not in self.runtime._auto_runtime
        assert "last_classification" not in self.device["auto"]
        assert "b" in self.runtime._source_runtime
        self.runtime._source_runtime["a"] = {"pending": True}
        self.runtime.configure_bermuda(self.config(a={"max_distance": 3}))
        assert "a" in self.runtime._source_runtime, "unchanged settings preserve buffers"
        self.runtime.configure_bermuda({"devices": {}})
        assert "a" not in self.runtime._source_runtime

    def test_migration_matches_id_or_casefolded_name_once(self):
        self.areas()
        self.device["presence_sources"] = {
            **sources.DEFAULTS,
            "confidence": 88,
            "sources": [
                {**bermuda(), "max_distance": 2},
                {**bermuda(), "entity_id": "sensor.watch_area", "area": "mAiN bEdRoOm"},
                {**bermuda(), "entity_id": "sensor.other_area", "area": "office"},
                boolean(),
            ],
        }
        self.runtime.data["devices"]["c"]["presence_sources"] = {
            **sources.DEFAULTS,
            "sources": [bermuda()],
        }
        module = sys.modules["tuner_under_test.presence.bermuda"]
        module.migrate(self.runtime)
        assert self.runtime.data["bermuda"] == {
            "devices": {
                "sensor.phone_area": {"radars": {"a": {"max_distance": 2}}},
                "sensor.watch_area": {"radars": {"a": {"max_distance": None}}},
            }
        }
        assert self.device["presence_sources"]["sources"] == [
            {**bermuda(), "entity_id": "sensor.other_area", "area": "office"},
            boolean(),
        ]
        assert self.device["presence_sources"]["confidence"] == 88
        assert self.runtime.data["devices"]["c"]["presence_sources"]["sources"] == [bermuda()]
        before = deepcopy(self.runtime.data)
        module.migrate(self.runtime)
        assert self.runtime.data == before

    def test_central_presence_uses_existing_start_and_end_buffers(self):
        self.areas()
        self.inventory()
        self.runtime.configure_bermuda(self.config(a={"max_distance": None}))
        device = self.device
        now = 2_000_000_000.0
        assert sources.estimate(self.runtime, device, "a", now)["state"] == "unknown"
        for second in (6, 12, 18):
            assert sources.estimate(self.runtime, device, "a", now + second)["state"] == "unknown"
        assert sources.estimate(self.runtime, device, "a", now + 24)["state"] == "present"
        self.runtime.configure_bermuda({"devices": {}})
        assert sources.estimate(self.runtime, device, "a", now + 25) is None

    def test_migration_keeps_existing_central_range(self):
        self.areas()
        self.runtime.configure_bermuda(self.config(a={"max_distance": 5}))
        self.device["presence_sources"] = {
            **sources.DEFAULTS,
            "sources": [{**bermuda(), "max_distance": 2}],
        }
        module = sys.modules["tuner_under_test.presence.bermuda"]
        module.migrate(self.runtime)
        assert self.runtime.data["bermuda"] == self.config(a={"max_distance": 5})
        assert self.device["presence_sources"]["sources"] == []
