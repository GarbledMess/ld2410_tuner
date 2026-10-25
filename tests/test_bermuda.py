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

    def room(self):
        self.areas()
        self.runtime.data["room_groups"] = {
            "zone:test": {
                "name": "Both",
                "kind": "zone",
                "area_id": None,
                "device_ids": ["a", "b", "c"],
            },
            "room:test": {
                "name": "Office",
                "kind": "area",
                "area_id": "office",
                "device_ids": ["b"],
            },
        }

    def test_room_and_radar_area_overrides_and_zone_reading(self):
        self.room()
        module = sys.modules["tuner_under_test.presence.bermuda"]
        values = self.config(c={"max_distance": None, "area": "office"})
        values["devices"]["sensor.phone_area"]["rooms"] = {
            "zone:test": {"max_distance": None, "area": None},
            "room:test": {"max_distance": 3, "area": "bedroom"},
        }
        assert module.validate(self.runtime, values) == values
        self.runtime.configure_bermuda(values)
        assert module.effective(self.runtime, "c") == [{**bermuda(), "area": "office"}]
        zone = module.effective(self.runtime, "zone:test", "rooms")
        assert zone == [{**bermuda(), "area": ["bedroom", "office"]}]
        for area, expected in (("office", True), ("bedroom", True), ("outside", False)):
            self.entity("sensor.phone_area", area)
            assert sources.source_state(self.hass, zone[0]) is expected
        assert module.effective(self.runtime, "room:test", "rooms") == [
            {**bermuda(), "max_distance": 3}
        ]

    def test_invalid_rooms_and_area_overrides_are_atomic(self):
        self.room()
        module = sys.modules["tuner_under_test.presence.bermuda"]
        for kind, target, config in (
            ("rooms", "missing", {"max_distance": None}),
            ("rooms", "zone:test", {"max_distance": 0}),
            ("rooms", "zone:test", {"max_distance": None, "area": "missing"}),
            ("radars", "a", {"max_distance": None, "area": ["office"]}),
            ("radars", "a", {"max_distance": None, "area": True}),
        ):
            with pytest.raises(ValueError):
                module.validate(
                    self.runtime,
                    {"devices": {"sensor.phone_area": {"radars": {}, kind: {target: config}}}},
                )

    def test_migration_moves_cross_area_room_and_unknown_text_after_old_marker(self):
        self.room()
        module = sys.modules["tuner_under_test.presence.bermuda"]
        self.runtime.data["bermuda_migrated"] = True
        self.device["presence_sources"] = {
            **sources.DEFAULTS,
            "sources": [{**bermuda(), "area": "oFfIcE", "max_distance": 4}, boolean()],
        }
        self.runtime.data["room_presence_sources"] = {
            "room:test": {
                **sources.DEFAULTS,
                "confidence": 77,
                "sources": [
                    {**bermuda(), "area": "Office"},
                    {**bermuda(), "entity_id": "sensor.watch_area", "area": "Lost room"},
                    boolean(),
                ],
            }
        }
        module.migrate(self.runtime)
        saved = self.runtime.data["bermuda"]["devices"]
        assert saved["sensor.phone_area"]["radars"]["a"] == {"max_distance": 4, "area": "office"}
        assert saved["sensor.phone_area"]["rooms"]["room:test"] == {
            "max_distance": None,
            "area": None,
        }
        assert saved["sensor.watch_area"]["rooms"]["room:test"]["area"] == "Lost room"
        assert self.device["presence_sources"]["sources"] == [boolean()]
        assert self.runtime.data["room_presence_sources"]["room:test"]["sources"] == [boolean()]
        assert self.runtime.data["room_presence_sources"]["room:test"]["confidence"] == 77
        before = deepcopy(self.runtime.data)
        module.migrate(self.runtime)
        assert self.runtime.data == before
        # A migrated unknown area remains saveable and readable, but cannot be added anew.
        assert module.validate(self.runtime, self.runtime.data["bermuda"])
        self.entity("sensor.watch_area", "Lost room")
        assert sources.source_state(
            self.hass, module.effective(self.runtime, "room:test", "rooms")[1]
        )

    def test_editors_reject_new_bermuda(self):
        self.room()
        room_sources = sys.modules["tuner_under_test.rooms.room_sources"]
        config = {**sources.DEFAULTS, "sources": [bermuda()]}
        with pytest.raises(ValueError, match="Bermuda section"):
            self.runtime.configure_presence_sources("a", config)
        with pytest.raises(ValueError, match="Bermuda section"):
            room_sources.configure_room_sources(self.runtime, "room:test", config)

    def test_central_room_sources_use_local_policy_and_tick_without_boolean_store(self):
        self.room()
        room_sources = sys.modules["tuner_under_test.rooms.room_sources"]
        values = self.config()
        values["devices"]["sensor.phone_area"]["rooms"] = {
            "room:test": {"max_distance": None, "area": None}
        }
        self.runtime.configure_bermuda(values)
        self.entity("sensor.phone_area", "office")
        assert room_sources.summary(self.runtime, "room:test")["raw_state"] == "present"
        room_sources.configure_room_sources(
            self.runtime,
            "room:test",
            {
                **sources.DEFAULTS,
                "confidence": 77,
                "mark_not_present": True,
                "start_buffer_seconds": 0,
                "end_buffer_seconds": 0,
            },
        )
        room_sources.sample_rooms(self.runtime, 1000)
        room_sources.sample_rooms(self.runtime, 1006)
        labels = self.runtime.data["room_labels"]["room:test"]
        assert labels[-1]["confidence"] == 0.77
        assert labels[-1]["state"] == "present"
        self.entity("sensor.phone_area", "outside")
        room_sources.sample_rooms(self.runtime, 1008)
        room_sources.sample_rooms(self.runtime, 1010)
        assert self.runtime.data["room_labels"]["room:test"][-1]["state"] == "not_present"

    def test_central_only_room_uses_default_buffers_and_removal_closes_label(self):
        self.room()
        room_sources = sys.modules["tuner_under_test.rooms.room_sources"]
        config = self.config()
        config["devices"]["sensor.phone_area"]["rooms"] = {
            "room:test": {"max_distance": None, "area": None}
        }
        self.runtime.configure_bermuda(config)
        self.entity("sensor.phone_area", "office")
        for now in range(1000, 1021, 2):
            room_sources.sample_rooms(self.runtime, now)
        assert not self.runtime.data.get("room_labels", {}).get("room:test")
        for now in range(1022, 1031, 2):
            room_sources.sample_rooms(self.runtime, now)
        labels = self.runtime.data["room_labels"]["room:test"]
        assert (labels[-1]["start"], labels[-1]["end"]) == (1010, 1020)
        assert labels[-1]["confidence"] == 0.9
        assert "room:test" not in self.runtime.data.get("room_presence_sources", {})
        revision = self.runtime.data["room_sensor_revision"]["room:test"]
        self.runtime.configure_bermuda(config)
        assert self.runtime.data["room_sensor_revision"]["room:test"] == revision
        assert "room:room:test" in self.runtime._source_runtime
        self.runtime.configure_bermuda({"devices": {}})
        assert "room:room:test" not in self.runtime._source_runtime
        assert "room:test" not in self.runtime._room_sensor_open
        room_sources.sample_rooms(self.runtime, 1040)
        assert labels[-1]["end"] == 1020

    def test_unknown_area_override_cannot_be_introduced_on_another_target(self):
        self.room()
        config = self.config(a={"max_distance": None, "area": "Lost room"})
        self.runtime.data["bermuda"] = deepcopy(config)
        assert self.runtime.configure_bermuda(config) == config
        config["devices"]["sensor.phone_area"]["radars"]["b"] = {
            "max_distance": None,
            "area": "Lost room",
        }
        with pytest.raises(ValueError, match="existing Home Assistant area"):
            self.runtime.configure_bermuda(config)

    def test_zone_migration_keeps_single_area_default_and_multi_area_specificity(self):
        self.room()
        module = sys.modules["tuner_under_test.presence.bermuda"]
        self.runtime.data["room_groups"]["zone:single"] = {
            "name": "Single",
            "kind": "zone",
            "area_id": None,
            "device_ids": ["a"],
        }
        self.runtime.data["room_presence_sources"] = {
            target: {**sources.DEFAULTS, "sources": [bermuda()]}
            for target in ("zone:single", "zone:test")
        }
        module.migrate(self.runtime)
        rooms = self.runtime.data["bermuda"]["devices"]["sensor.phone_area"]["rooms"]
        assert rooms["zone:single"]["area"] is None
        assert rooms["zone:test"]["area"] == "bedroom"

    def test_lab_style_radar_without_registry_has_central_and_room_snapshot(self):
        module = sys.modules["tuner_under_test.presence.bermuda"]
        legacy = {**sources.DEFAULTS, "sources": [{**bermuda(), "max_distance": 3}]}
        self.runtime.data["devices"] = {"probe": {"presence_sources": deepcopy(legacy)}}
        self.runtime.data["room_groups"] = {
            "zone:probe": {
                "name": "Probe",
                "kind": "zone",
                "area_id": None,
                "device_ids": ["probe"],
            }
        }
        self.runtime.data["room_presence_sources"] = {"zone:probe": deepcopy(legacy)}
        module.migrate(self.runtime)
        snapshot = self.runtime.snapshot()
        assert "probe" not in snapshot["devices"]
        saved = snapshot["bermuda"]["devices"]["sensor.phone_area"]
        assert saved["radars"]["probe"] == {"max_distance": 3, "area": "bedroom"}
        assert saved["rooms"]["zone:probe"] == {"max_distance": 3, "area": "bedroom"}
        assert snapshot["rooms"]["groups"]["zone:probe"]["presence_sources"]["sources"] == []
        assert snapshot["bermuda_radars"]["probe"]["area_id"] is None

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
                "sensor.phone_area": {
                    "radars": {
                        "a": {"max_distance": 2, "area": None},
                        "c": {"max_distance": None, "area": "bedroom"},
                    }
                },
                "sensor.watch_area": {"radars": {"a": {"max_distance": None, "area": None}}},
                "sensor.other_area": {"radars": {"a": {"max_distance": None, "area": "office"}}},
            }
        }
        assert self.device["presence_sources"]["sources"] == [boolean()]
        assert self.device["presence_sources"]["confidence"] == 88
        assert self.runtime.data["devices"]["c"]["presence_sources"]["sources"] == []
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
        assert self.runtime.data["bermuda_migration_conflicts"] == [
            {
                "target_kind": "radars",
                "target_id": "a",
                "source": {**bermuda(), "max_distance": 2},
            }
        ]
        before = deepcopy(self.runtime.data)
        module.migrate(self.runtime)
        assert self.runtime.data == before
