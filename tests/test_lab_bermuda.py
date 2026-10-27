"""Check the disposable lab probe's synthetic inputs and migration assertions locally."""

import asyncio
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
        "bermuda": {"devices": ["sensor.lab_phone_area"], "max_distance": None, "rooms": {}},
        "devices": {"probe": {"presence_sources": {"sources": []}}},
        "rooms": {"groups": {"zone:probe": {"presence_sources": {"sources": []}}}},
    }
    module["assert_migrated"](snapshot)
    # HA omits cards for stored radars with no discoverable energy entities.
    no_registry = deepcopy(snapshot)
    no_registry["devices"] = {}
    module["assert_migrated"](no_registry)
    bad = deepcopy(snapshot)
    bad["rooms"]["groups"]["zone:probe"]["presence_sources"]["sources"] = [{"kind": "bermuda"}]
    with pytest.raises(AssertionError):
        module["assert_migrated"](bad)


def test_lab_commands_use_shared_settings_and_reject_invalid_payloads(monkeypatch):
    module = probe(monkeypatch)
    saved = {"devices": [module["ENTITY"]], "max_distance": 2.5, "rooms": {}}
    calls = []

    async def command(socket, ident, payload):
        calls.append(payload)
        kind = payload["type"].split("/")[-1]
        if kind == "configure_bermuda":
            values = payload["settings"]
            if values["max_distance"] == 0 or "zone:probe" in values["rooms"]:
                return {"success": False, "error": {"code": "invalid_request"}}
            assert values == saved
            return {"success": True, "result": deepcopy(saved)}
        result = {"snapshot": {"bermuda": saved}, "bermuda_devices": [], "recording_days": []}
        return {"success": True, "result": result[kind]}

    module["check_commands"].__globals__["command"] = command
    snapshot = {"bermuda": {**saved, "max_distance": None}}
    before = deepcopy(snapshot)
    asyncio.run(module["check_commands"](object(), snapshot))
    assert snapshot == before
    configured = [x["settings"] for x in calls if x["type"].endswith("/configure_bermuda")]
    assert configured == [
        saved,
        {**saved, "max_distance": 0},
        {**saved, "rooms": {"zone:probe": {"max_distance": None}}},
    ]
    assert sum(x["type"].endswith("/snapshot") for x in calls) == 2
