"""Central Bermuda assignments use synthetic registries and states only."""

import sys
import types
import unittest
from copy import deepcopy
from unittest.mock import patch

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

    def legacy_config(self, **radars):
        return {"devices": {"sensor.phone_area": {"radars": radars}}}

    def config(self, **changes):
        return {
            "devices": ["sensor.phone_area"],
            "max_distance": 3,
            "rooms": {"area:office": {"max_distance": None}},
            **changes,
        }

    def shared_room(self):
        self.room()
        self.inventory()
        self.runtime.data["room_groups"]["area:office"] = {
            "name": "Office",
            "kind": "area",
            "area_id": "office",
            "device_ids": ["b"],
        }

    def test_shared_room_settings_and_range_inheritance(self):
        self.shared_room()
        module = sys.modules["tuner_under_test.presence.bermuda"]
        values = self.config()
        assert self.runtime.configure_bermuda(values) == values
        expected = {**bermuda(), "area": "office", "max_distance": 3}
        assert module.effective(self.runtime, "area:office") == [expected]
        assert module.effective(self.runtime, "zone:test") == []
        for distance in (0.01, 100):
            assert (
                self.runtime.configure_bermuda(self.config(max_distance=distance))["max_distance"]
                == distance
            )
        for distance in (0.01, 4, 100, None):
            self.runtime.configure_bermuda(
                self.config(max_distance=None, rooms={"area:office": {"max_distance": distance}})
            )
            source = {**bermuda(), "area": "office"}
            if distance is not None:
                source["max_distance"] = distance
            assert module.effective(self.runtime, "area:office") == [source]
        self.runtime.configure_bermuda(self.config(rooms={"area:office": {"max_distance": 4}}))
        assert module.effective(self.runtime, "area:office")[0]["max_distance"] == 4
        values["devices"].clear()
        saved = module.settings(self.runtime)
        saved["rooms"].clear()
        assert module.settings(self.runtime)["devices"] == ["sensor.phone_area"]
        assert module.settings(self.runtime)["rooms"]

    def test_shared_room_validation_is_atomic(self):
        self.shared_room()
        good = self.config()
        self.runtime.configure_bermuda(good)
        self.runtime._room_sensor_open["area:office"] = {"written": True, "end": 1000}
        self.runtime._source_runtime["room:area:office"] = {"pending": True}
        bad = [None, [], {}, {"devices": {}}, {**good, "extra": 1}]
        bad += [
            self.config(devices=value)
            for value in (
                {},
                "sensor.phone_area",
                ["sensor.phone_area"] * 2,
                ["bad"],
                ["sensor.no_sibling"],
                [True],
                [[]],
            )
        ]
        bad += [
            self.config(rooms=value)
            for value in (
                [],
                {"zone:test": {"max_distance": None}},
                {"a": {"max_distance": None}},
                {"room:test": {"max_distance": None}},
                {"missing": {"max_distance": None}},
                {"area:office": None},
                {"area:office": {}},
                {"area:office": {"max_distance": None, "area": "office"}},
            )
        ]
        for distance in (0, -1, 101, True, "3", float("nan"), float("inf")):
            bad.extend(
                (
                    self.config(max_distance=distance),
                    self.config(rooms={"area:office": {"max_distance": distance}}),
                )
            )
        bad += [
            self.config(
                rooms={"area:office": {"max_distance": 3}, "zone:test": {"max_distance": None}}
            ),
            self.config(devices=["sensor.phone_area", "sensor.no_sibling"]),
        ]
        before = deepcopy(self.runtime.data)
        buffers = deepcopy(self.runtime._source_runtime)
        opened = deepcopy(self.runtime._room_sensor_open)
        for value in bad:
            with self.subTest(value=value), pytest.raises(ValueError):
                self.runtime.configure_bermuda(value)
            assert self.runtime.data == before
            assert self.runtime._source_runtime == buffers
            assert self.runtime._room_sensor_open == opened
        self.registry.entities.clear()
        assert self.runtime.configure_bermuda(good) == good

    def test_shared_room_requires_recording_members(self):
        self.shared_room()
        self.runtime.configure_bermuda(self.config())
        self.runtime.data["devices"]["b"]["recording_enabled"] = False
        with pytest.raises(ValueError):
            self.runtime.configure_bermuda(self.config())
        module = sys.modules["tuner_under_test.presence.bermuda"]
        self.runtime.data["bermuda"] = self.config()
        assert module.effective(self.runtime, "area:office") == []

    def test_shared_room_rejects_deleted_registry_area(self):
        self.shared_room()
        self.runtime.configure_bermuda(self.config())
        ar = sys.modules["homeassistant.helpers.area_registry"]
        registry = types.SimpleNamespace(async_list_areas=lambda: [])
        with patch.object(ar, "async_get", return_value=registry):
            with pytest.raises(ValueError, match="current area room"):
                self.runtime.configure_bermuda(self.config())
            module = sys.modules["tuner_under_test.presence.bermuda"]
            assert module.effective(self.runtime, "area:office") == []

    def test_migration_deleted_registry_area_preserves_conflict(self):
        self.shared_room()
        self.runtime.data["bermuda"] = {
            "devices": {
                "sensor.phone_area": {"radars": {}, "rooms": {"area:office": {"max_distance": 3}}}
            }
        }
        self.runtime.data["bermuda_targets_migrated"] = True
        ar = sys.modules["homeassistant.helpers.area_registry"]
        registry = types.SimpleNamespace(async_list_areas=lambda: [])
        module = sys.modules["tuner_under_test.presence.bermuda"]
        with patch.object(ar, "async_get", return_value=registry):
            module.migrate(self.runtime)
        assert self.runtime.data["bermuda"] == self.config(max_distance=None, rooms={})
        assert self.runtime.data["bermuda_migration_conflicts"] == [
            {
                "target_kind": "rooms",
                "target_id": "area:office",
                "source": {**bermuda(), "area": "office", "max_distance": 3},
            }
        ]

    def test_shared_room_or_disabled_and_forget(self):
        self.shared_room()
        module = sys.modules["tuner_under_test.presence.bermuda"]
        room_sources = sys.modules["tuner_under_test.rooms.room_sources"]
        self.runtime.data["bermuda"] = self.config(devices=["sensor.watch_area"])
        self.runtime.configure_bermuda(
            self.config(devices=["sensor.watch_area", "sensor.phone_area"])
        )
        self.entity("sensor.watch_area", "outside")
        self.entity("sensor.phone_area", "office")
        self.entity("sensor.renamed_distance", "3")
        assert room_sources.summary(self.runtime, "area:office")["raw_state"] == "present"
        assert sources._read_sources(self.runtime, self.device)["sources"] == []
        for changes in ({"devices": []}, {"rooms": {}}):
            self.runtime.configure_bermuda(self.config(**changes))
            assert module.effective(self.runtime, "area:office") == []
        self.runtime.configure_bermuda(self.config())
        room_sources.forget(self.runtime, "area:office")
        assert module.settings(self.runtime) == self.config(rooms={})

    def test_shared_room_snapshot(self):
        self.shared_room()
        self.runtime.configure_bermuda(self.config())
        self.runtime.refresh_devices = lambda registry: None
        snapshot = self.runtime.snapshot()
        assert snapshot["bermuda"] == self.config()
        assert snapshot["bermuda_devices"] == self.runtime.bermuda_devices()
        assert "bermuda_radars" not in snapshot

    def test_migration_shared_rooms_unlimited_wins(self):
        self.shared_room()
        module = sys.modules["tuner_under_test.presence.bermuda"]
        self.runtime.data["room_groups"]["area:bedroom"] = {
            "name": "Bedroom",
            "kind": "area",
            "area_id": "bedroom",
            "device_ids": ["a"],
        }
        self.runtime.data["room_groups"]["zone:test"]["area_id"] = "office"
        central = {
            "devices": {
                "sensor.phone_area": {
                    "radars": {
                        "a": {"max_distance": 2},
                        "b": {"area": "Office", "max_distance": 4},
                    },
                    "rooms": {
                        "area:office": {"max_distance": 2},
                        "zone:test": {"max_distance": 3},
                        "area:bedroom": {"area": "OFFICE", "max_distance": 3},
                    },
                },
                "sensor.watch_area": {"radars": {"a": {"area": "office", "max_distance": 2}}},
            }
        }
        for distance in (4, None):
            self.runtime.data.pop("bermuda_rooms_migrated", None)
            self.runtime.data["bermuda_targets_migrated"] = True
            self.runtime.data["bermuda"] = deepcopy(central)
            self.runtime.data["bermuda"]["devices"]["sensor.phone_area"]["radars"]["b"][
                "max_distance"
            ] = distance
            module.migrate(self.runtime)
            assert self.runtime.data["bermuda"] == {
                "devices": ["sensor.phone_area", "sensor.watch_area"],
                "max_distance": None,
                "rooms": {
                    "area:bedroom": {"max_distance": 2},
                    "area:office": {"max_distance": distance},
                },
            }
            assert self.runtime.data.get("bermuda_rooms_migrated") is True
            before = deepcopy(self.runtime.data)
            module.migrate(self.runtime)
            assert self.runtime.data == before

    def test_migration_preserves_unresolved_and_old_conflicts(self):
        self.shared_room()
        module = sys.modules["tuner_under_test.presence.bermuda"]
        previous = {"target_kind": "radars", "target_id": "old", "source": bermuda()}
        self.runtime.data["bermuda_migration_conflicts"] = [deepcopy(previous)]
        self.runtime.data["bermuda_targets_migrated"] = True
        targets = {
            "c": {"max_distance": 2},
            "missing": {"max_distance": None},
            "a": {"area": "Lost room", "max_distance": 4},
        }
        self.runtime.data["bermuda"] = {
            "devices": {
                "sensor.phone_area": {
                    "radars": targets,
                    "rooms": {"zone:test": {"max_distance": 3}},
                },
                "sensor.off_area": {"radars": {}, "rooms": {}},
            }
        }
        self.device["presence_sources"] = {**sources.DEFAULTS, "confidence": 77}
        self.runtime.data["room_labels"] = {
            "area:office": [{"source": "sensor", "start": 1, "end": 2, "state": "present"}]
        }
        labels = deepcopy(self.runtime.data["room_labels"])
        module.migrate(self.runtime)
        assert self.runtime.data["bermuda"] == self.config(max_distance=None, rooms={})
        conflicts = self.runtime.data["bermuda_migration_conflicts"]
        assert conflicts[0] == previous
        assert len(conflicts) == 5
        for item in conflicts[1:]:
            assert item["source"]["entity_id"] == "sensor.phone_area"
            assert item["source"]["kind"] == "bermuda"
            assert "area" in item["source"]
        assert next(x for x in conflicts if x["target_id"] == "a")["source"] == {
            **bermuda(),
            "area": "Lost room",
            "max_distance": 4,
        }
        assert self.runtime.data["room_labels"] == labels
        assert not self.runtime.data.get("room_presence_sources")

    def test_migration_guards_empty_and_already_new_settings(self):
        self.shared_room()
        module = sys.modules["tuner_under_test.presence.bermuda"]
        for guards in (
            {},
            {"bermuda_migrated": True},
            {"bermuda_targets_migrated": True},
            {"bermuda_migrated": True, "bermuda_targets_migrated": True},
        ):
            for values in (None, self.config(devices=["sensor.unavailable_area"])):
                with self.subTest(guards=guards, values=values):
                    for key in (
                        "bermuda",
                        "bermuda_migrated",
                        "bermuda_targets_migrated",
                        "bermuda_rooms_migrated",
                    ):
                        self.runtime.data.pop(key, None)
                    self.runtime.data.update(guards)
                    if values is not None:
                        self.runtime.data["bermuda"] = deepcopy(values)
                    module.migrate(self.runtime)
                    assert self.runtime.data["bermuda"] == (
                        values or {"devices": [], "max_distance": None, "rooms": {}}
                    )
                    assert self.runtime.data.get("bermuda_rooms_migrated")
                    before = deepcopy(self.runtime.data)
                    module.migrate(self.runtime)
                    assert self.runtime.data == before

    def test_migration_retains_new_device_order_without_discovery(self):
        self.shared_room()
        module = sys.modules["tuner_under_test.presence.bermuda"]
        self.registry.entities.clear()
        saved = self.config(devices=["sensor.watch_area", "sensor.phone_area"])
        self.runtime.data["bermuda"] = deepcopy(saved)
        module.migrate(self.runtime)
        assert self.runtime.data["bermuda"] == saved

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

    def test_migration_moves_cross_area_room_and_unknown_text_after_old_marker(self):
        self.shared_room()
        module = sys.modules["tuner_under_test.presence.bermuda"]
        self.runtime.data["bermuda_migrated"] = True
        self.device["presence_sources"] = {
            **sources.DEFAULTS,
            "sources": [{**bermuda(), "area": "oFfIcE", "max_distance": 4}, boolean()],
        }
        self.runtime.data["room_presence_sources"] = {
            "area:office": {
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
        assert self.runtime.data["bermuda"] == self.config(
            devices=["sensor.phone_area", "sensor.watch_area"], max_distance=None
        )
        assert self.device["presence_sources"]["sources"] == [boolean()]
        assert self.runtime.data["room_presence_sources"]["area:office"]["sources"] == [boolean()]
        assert self.runtime.data["room_presence_sources"]["area:office"]["confidence"] == 77
        assert self.runtime.data["bermuda_migration_conflicts"] == [
            {
                "target_kind": "rooms",
                "target_id": "area:office",
                "source": {**bermuda(), "entity_id": "sensor.watch_area", "area": "Lost room"},
            }
        ]
        before = deepcopy(self.runtime.data)
        module.migrate(self.runtime)
        assert self.runtime.data == before
        assert module.validate(self.runtime, self.runtime.data["bermuda"])

    def test_editors_reject_new_bermuda(self):
        self.room()
        room_sources = sys.modules["tuner_under_test.rooms.room_sources"]
        config = {**sources.DEFAULTS, "sources": [bermuda()]}
        with pytest.raises(ValueError, match="Bermuda section"):
            self.runtime.configure_presence_sources("a", config)
        with pytest.raises(ValueError, match="Bermuda section"):
            room_sources.configure_room_sources(self.runtime, "room:test", config)

    def test_central_room_sources_use_local_policy_and_tick_without_boolean_store(self):
        self.shared_room()
        room_sources = sys.modules["tuner_under_test.rooms.room_sources"]
        config = self.config(max_distance=None)
        self.runtime.configure_bermuda(config)
        self.entity("sensor.phone_area", "office")
        assert room_sources.summary(self.runtime, "area:office")["raw_state"] == "present"
        room_sources.configure_room_sources(
            self.runtime,
            "area:office",
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
        labels = self.runtime.data["room_labels"]["area:office"]
        assert labels[-1]["confidence"] == 0.77
        assert labels[-1]["state"] == "present"
        self.entity("sensor.phone_area", "outside")
        room_sources.sample_rooms(self.runtime, 1008)
        room_sources.sample_rooms(self.runtime, 1010)
        labels = self.runtime.data["room_labels"]["area:office"]
        assert [(item["state"], item["end"]) for item in labels] == [("present", 1008)]
        assert room_sources.summary(self.runtime, "area:office")["raw_state"] == "unknown"

    def test_central_only_room_uses_default_buffers_and_removal_closes_label(self):
        self.shared_room()
        room_sources = sys.modules["tuner_under_test.rooms.room_sources"]
        config = self.config(max_distance=None)
        self.runtime.configure_bermuda(config)
        self.entity("sensor.phone_area", "office")
        for now in range(1000, 1021, 2):
            room_sources.sample_rooms(self.runtime, now)
        assert not self.runtime.data.get("room_labels", {}).get("area:office")
        for now in range(1022, 1031, 2):
            room_sources.sample_rooms(self.runtime, now)
        labels = self.runtime.data["room_labels"]["area:office"]
        assert (labels[-1]["start"], labels[-1]["end"]) == (1010, 1020)
        assert labels[-1]["confidence"] == 0.9
        assert "area:office" not in self.runtime.data.get("room_presence_sources", {})
        revision = self.runtime.data["room_sensor_revision"]["area:office"]
        self.runtime.configure_bermuda(config)
        assert self.runtime.data["room_sensor_revision"]["area:office"] == revision
        assert "room:area:office" in self.runtime._source_runtime
        self.runtime.configure_bermuda(self.config(devices=[], rooms={}))
        assert "room:area:office" not in self.runtime._source_runtime
        assert "area:office" not in self.runtime._room_sensor_open
        room_sources.sample_rooms(self.runtime, 1040)
        assert labels[-1]["end"] == 1020

    def test_zone_migration_keeps_explicit_source_area_without_member_inference(self):
        self.shared_room()
        module = sys.modules["tuner_under_test.presence.bermuda"]
        self.runtime.data["room_presence_sources"] = {
            "zone:test": {**sources.DEFAULTS, "sources": [{**bermuda(), "area": "Office"}]}
        }
        module.migrate(self.runtime)
        assert self.runtime.data["bermuda"] == self.config(max_distance=None)
        assert self.runtime.data["room_presence_sources"]["zone:test"]["sources"] == []

    def test_lab_style_radar_without_registry_has_conflicts_and_room_snapshot(self):
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
        assert snapshot["bermuda"] == self.config(max_distance=None, rooms={})
        assert snapshot["rooms"]["groups"]["zone:probe"]["presence_sources"]["sources"] == []
        assert "bermuda_radars" not in snapshot
        assert self.runtime.data["bermuda_migration_conflicts"] == [
            {"target_kind": kind, "target_id": target, "source": {**bermuda(), "max_distance": 3}}
            for kind, target in (("radars", "probe"), ("rooms", "zone:probe"))
        ]

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

    def test_migration_matches_id_or_casefolded_name_once(self):
        self.shared_room()
        self.runtime.data["room_groups"]["area:bedroom"] = {
            "kind": "area",
            "area_id": "bedroom",
            "name": "Bedroom",
            "device_ids": ["a"],
        }
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
            "devices": ["sensor.other_area", "sensor.phone_area", "sensor.watch_area"],
            "max_distance": None,
            "rooms": {
                "area:bedroom": {"max_distance": None},
                "area:office": {"max_distance": None},
            },
        }
        assert self.device["presence_sources"]["sources"] == [boolean()]
        assert self.device["presence_sources"]["confidence"] == 88
        assert self.runtime.data["devices"]["c"]["presence_sources"]["sources"] == []
        before = deepcopy(self.runtime.data)
        module.migrate(self.runtime)
        assert self.runtime.data == before

    def test_migration_keeps_existing_central_range(self):
        self.shared_room()
        self.runtime.data["room_groups"]["area:bedroom"] = {
            "kind": "area",
            "area_id": "bedroom",
            "name": "Bedroom",
            "device_ids": ["a"],
        }
        self.runtime.data["bermuda"] = self.legacy_config(a={"max_distance": 5})
        self.device["presence_sources"] = {
            **sources.DEFAULTS,
            "sources": [{**bermuda(), "max_distance": 2}],
        }
        module = sys.modules["tuner_under_test.presence.bermuda"]
        module.migrate(self.runtime)
        assert self.runtime.data["bermuda"] == self.config(
            max_distance=None, rooms={"area:bedroom": {"max_distance": 5}}
        )
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
