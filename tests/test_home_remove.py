"""Removing a replaced radar on the real-shaped home: everything stored for it is deleted and
it leaves every room, so it cannot pollute the radars that replaced it."""

import os

import pytest
import real_home
import real_shape  # noqa: F401  (the real-shaped home's clock)
from real_home import WebsocketError

RADAR, OTHER = "living_room_1", "living_room_2"


async def _recording(home):
    for radar in (RADAR, OTHER):
        await home.ws("set_recording", device_id=radar, enabled=True)


def _deliver(home, radar, values):
    for value in values:
        home.clock.moved(1.5)
        home.report(radar, "g1_move", value)


def _members(snapshot, group_id):
    return snapshot["rooms"]["groups"].get(group_id, {}).get("device_ids")


async def _zone(home, name, device_ids):
    await home.ws("configure_room", name=name, area_id="living_room", device_ids=device_ids)
    snapshot = await home.ws("snapshot")
    return next(key for key, item in snapshot["rooms"]["groups"].items() if item["name"] == name)


def _leave_home_assistant(home, radar):
    entities = home.hass.entity_registry.entities
    for entity_id in [key for key, item in entities.items() if item.device_id == radar]:
        entities.pop(entity_id)
    home.runtime.handle_registry_update(None)


async def _stored(home, radar):
    await home.save()
    for task in tuple(getattr(home.runtime, "_storage_io_tasks", ())):
        await task
    return os.path.isdir(home.runtime._days.folder(radar))


def test_a_radar_in_use_cannot_be_removed(play):
    async def scenario(home):
        await _recording(home)
        with pytest.raises(WebsocketError, match="Exclude this radar first"):
            await home.ws("remove_radar", device_id=RADAR)
        with pytest.raises(WebsocketError, match="Unknown"):
            await home.ws("remove_radar", device_id="no_such_radar")
        assert RADAR in (await home.ws("snapshot"))["devices"]

    play(scenario)


def test_removing_an_excluded_radar_deletes_its_data_and_takes_it_out_of_its_zones(play):
    async def scenario(home):
        await _recording(home)
        _deliver(home, RADAR, [61, 62, 63])
        zone = await _zone(home, "Sofa corner", [RADAR, OTHER])
        selection = {"device_ids": [RADAR, OTHER], "group_ids": [zone]}
        await home.ws("save_label_template", name="Both", **selection)
        home.runtime.data.setdefault("room_assessments", {})[zone] = {"old": True}
        assert await _stored(home, RADAR)

        await home.ws("set_excluded", device_id=RADAR, excluded=True)
        assert await home.ws("remove_radar", device_id=RADAR) == {"ok": True}

        data = home.runtime.data
        assert data["room_groups"][zone]["device_ids"] == [OTHER]
        assert zone not in data.get("room_assessments", {})
        assert data["label_selections"]["templates"]["Both"]["device_ids"] == [OTHER]
        device = data["devices"][RADAR]
        assert device["excluded"]["recording"] is False
        assert not device.get("history_labels") and not device.get("histograms")
        assert not await _stored(home, RADAR)

        snapshot = await home.ws("snapshot")
        assert [item["device_id"] for item in snapshot["excluded_radars"]] == [RADAR]
        assert snapshot["departed_radars"] == []
        assert _members(snapshot, zone) == [OTHER]

        # Included again it starts over with nothing from before.
        await home.ws("set_excluded", device_id=RADAR, excluded=False)
        snapshot = await home.ws("snapshot")
        assert snapshot["devices"][RADAR]["recording_enabled"] is False
        await home.ws("set_recording", device_id=RADAR, enabled=True)
        snapshot = await home.ws("snapshot")
        assert _members(snapshot, "area:living_room") == [RADAR, OTHER]
        assert _members(snapshot, zone) == [OTHER]

    play(scenario)


def test_a_radar_gone_from_home_assistant_is_listed_and_removed_entirely(play):
    async def scenario(home):
        await _recording(home)
        _deliver(home, RADAR, [61, 62])
        zone = await _zone(home, "Sofa corner", [RADAR, OTHER])
        name = (await home.ws("snapshot"))["devices"][RADAR]["name"]
        _leave_home_assistant(home, RADAR)

        snapshot = await home.ws("snapshot")
        departed = {item["device_id"]: item for item in snapshot["departed_radars"]}
        assert set(departed) == {RADAR}
        assert departed[RADAR]["name"] == name
        # A saved zone still holds its old radar until it is removed.
        assert _members(snapshot, zone) == [RADAR, OTHER]
        assert _members(snapshot, "area:living_room") == [OTHER]

        await home.ws("remove_radar", device_id=RADAR)
        assert RADAR not in home.runtime.data["devices"]
        assert not await _stored(home, RADAR)
        snapshot = await home.ws("snapshot")
        assert snapshot["departed_radars"] == []
        assert snapshot["excluded_radars"] == []
        assert _members(snapshot, zone) == [OTHER]

    play(scenario)


def test_removal_refuses_to_leave_a_zone_without_radars(play):
    async def scenario(home):
        await _recording(home)
        zone = await _zone(home, "Reading chair", [RADAR])
        await home.ws("set_excluded", device_id=RADAR, excluded=True)
        with pytest.raises(WebsocketError, match="Reading chair has no other radar"):
            await home.ws("remove_radar", device_id=RADAR)
        assert home.runtime.data["room_groups"][zone]["device_ids"] == [RADAR]
        assert await _stored(home, RADAR)

    play(scenario)


def test_an_emptied_area_override_falls_back_to_the_area_radars(play):
    async def scenario(home):
        await _recording(home)
        await home.ws(
            "configure_room",
            group_id="area:living_room",
            name="Living room",
            area_id="living_room",
            device_ids=[RADAR],
        )
        await home.ws("set_excluded", device_id=RADAR, excluded=True)
        await home.ws("remove_radar", device_id=RADAR)
        assert "area:living_room" not in home.runtime.data.get("room_groups", {})
        assert _members(await home.ws("snapshot"), "area:living_room") == [OTHER]

    play(scenario)


def test_joint_results_that_set_its_thresholds_are_dropped(play):
    async def scenario(home):
        await _recording(home)
        learning = home.runtime.data.setdefault("room_learning", {})
        learning["kept"] = {"slots": {"manual": {"thresholds": {OTHER: {"g1_move": 30}}}}}
        learning["dropped"] = {
            "slots": {"manual": {"thresholds": {RADAR: {"g1_move": 30}, OTHER: {}}}},
            "job": {"status": "done"},
        }
        await home.ws("set_excluded", device_id=RADAR, excluded=True)
        await home.ws("remove_radar", device_id=RADAR)
        assert learning["dropped"] == {"slots": {}, "job": {"status": "done"}}
        assert "manual" in learning["kept"]["slots"]

    play(scenario)


def test_a_removed_radar_that_returns_to_home_assistant_starts_over_paused(play):
    async def scenario(home):
        await _recording(home)
        _deliver(home, RADAR, [61, 62])
        _leave_home_assistant(home, RADAR)
        await home.ws("remove_radar", device_id=RADAR)
        assert not await _stored(home, RADAR)

    home = play(scenario)

    async def read_back(restarted):
        snapshot = await restarted.ws("snapshot")
        assert snapshot["devices"][RADAR]["recording_enabled"] is False
        assert not restarted.runtime.data["devices"][RADAR].get("history_labels")
        assert not await _stored(restarted, RADAR)
        assert _members(snapshot, "area:living_room") == [OTHER]

    second = real_home.Home(home.config)
    second.clock.now = home.clock.now
    with real_home.worker_on_transport(second):
        real_home.run_on_clock(second.clock, lambda: real_home.lifetime(second, read_back))
