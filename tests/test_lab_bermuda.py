"""Check the disposable lab probe's synthetic inputs and migration assertions locally."""

import runpy
import sys
import types
from copy import deepcopy
from pathlib import Path

import pytest


def probe(monkeypatch):
    path = Path(__file__).parent / "lab"
    monkeypatch.syspath_prepend(str(path))
    # HTTP is an external boundary; these local tests only exercise pure probe helpers.
    monkeypatch.setitem(sys.modules, "aiohttp", types.ModuleType("aiohttp"))
    return runpy.run_path(str(path / "bermuda_checks.py"))


def test_lab_store_contains_both_legacy_sources(monkeypatch):
    module = probe(monkeypatch)
    store = module["synthetic_store"]()
    data = store["data"]
    radar = data["devices"]["probe"]["presence_sources"]["sources"][0]
    room = data["room_presence_sources"]["zone:probe"]["sources"][0]
    assert radar["kind"] == room["kind"] == "bermuda"
    assert radar["max_distance"] == 3
    assert room["max_distance"] == 4
    assert data["room_groups"]["zone:probe"]["area_id"] is None
    assert data["room_groups"]["zone:probe"]["device_ids"] == ["probe"]
    assert "bermuda" not in data


def test_lab_migration_assertion_detects_unmigrated_sources(monkeypatch):
    module = probe(monkeypatch)
    snapshot = {
        "bermuda": {
            "devices": {
                "sensor.lab_phone_area": {
                    "radars": {"probe": {"area": "Lab unknown area", "max_distance": 3}},
                    "rooms": {"zone:probe": {"area": "Lab unknown area", "max_distance": 4}},
                }
            }
        },
        "devices": {"probe": {"presence_sources": {"sources": []}}},
        "rooms": {"groups": {"zone:probe": {"presence_sources": {"sources": []}}}},
    }
    module["assert_migrated"](snapshot)
    # HA omits cards for stored radars with no discoverable energy entities.
    no_registry = deepcopy(snapshot)
    no_registry["devices"] = {}
    no_registry["bermuda_radars"] = {"probe": {"area_id": None, "reason": "No area"}}
    module["assert_migrated"](no_registry)
    bad = deepcopy(snapshot)
    bad["rooms"]["groups"]["zone:probe"]["presence_sources"]["sources"] = [{"kind": "bermuda"}]
    with pytest.raises(AssertionError):
        module["assert_migrated"](bad)
