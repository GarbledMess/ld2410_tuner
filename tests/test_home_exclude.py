"""Excluding a radar on the real-shaped home: it is left out of everything until included again,
and nothing it had is lost."""

from datetime import UTC, datetime

import pytest
import real_home
import real_shape
from real_home import WebsocketError

TODAY = datetime.fromtimestamp(real_shape.NOW, UTC).date().isoformat()
RADAR, OTHER = "living_room_1", "living_room_2"


async def _rows(home, radar):
    return (await home.ws("recording_rows", device_id=radar, date=TODAY, limit=1))["total"]


def _deliver(home, radar, values):
    for value in values:
        home.clock.moved(1.5)
        home.report(radar, "g1_move", value)


async def _recording(home):
    for radar in (RADAR, OTHER):
        await home.ws("set_recording", device_id=radar, enabled=True)


def _members(snapshot, group_id):
    return snapshot["rooms"]["groups"].get(group_id, {}).get("device_ids")


def test_an_excluded_radar_is_tucked_away_records_nothing_and_leaves_its_room(play):
    async def scenario(home):
        await _recording(home)
        before = await home.ws("snapshot")
        assert before["devices"][RADAR]["recording_enabled"] is True
        assert _members(before, "area:living_room") == [RADAR, OTHER]

        assert await home.ws("set_excluded", device_id=RADAR, excluded=True) == {"excluded": True}
        snapshot = await home.ws("snapshot")
        assert RADAR not in snapshot["devices"]
        tucked = {item["device_id"]: item for item in snapshot["excluded_radars"]}
        assert set(tucked) == {RADAR}
        assert tucked[RADAR]["name"] == before["devices"][RADAR]["name"]
        assert _members(snapshot, "area:living_room") == [OTHER]

        rows = await _rows(home, RADAR)
        _deliver(home, RADAR, [61, 62])
        await home.save()
        assert await _rows(home, RADAR) == rows

    play(scenario)


def test_including_a_radar_again_restores_it_as_it_was(play):
    async def scenario(home):
        await _recording(home)
        labels = len(home.runtime.data["devices"][RADAR].get("history_labels", []))
        await home.ws("set_excluded", device_id=RADAR, excluded=True)
        assert await home.ws("set_excluded", device_id=RADAR, excluded=False) == {"excluded": False}
        snapshot = await home.ws("snapshot")
        assert snapshot["devices"][RADAR]["recording_enabled"] is True
        assert snapshot["excluded_radars"] == []
        assert _members(snapshot, "area:living_room") == [RADAR, OTHER]
        assert len(home.runtime.data["devices"][RADAR].get("history_labels", [])) == labels

        rows = await _rows(home, RADAR)
        _deliver(home, RADAR, [63, 64])
        await home.save()
        assert await _rows(home, RADAR) > rows

    play(scenario)


def test_a_radar_whose_recording_was_off_comes_back_with_recording_off(play):
    async def scenario(home):
        await _recording(home)
        await home.ws("set_excluded", device_id="garage_1", excluded=True)
        await home.ws("set_excluded", device_id="garage_1", excluded=False)
        snapshot = await home.ws("snapshot")
        assert snapshot["devices"]["garage_1"]["recording_enabled"] is False

    play(scenario)


def test_an_excluded_radar_stays_excluded_through_a_restart(play):
    async def scenario(home):
        await _recording(home)
        await home.ws("set_excluded", device_id=RADAR, excluded=True)

    home = play(scenario)

    async def read_back(restarted):
        snapshot = await restarted.ws("snapshot")
        assert RADAR not in snapshot["devices"]
        assert [item["device_id"] for item in snapshot["excluded_radars"]] == [RADAR]
        assert _members(snapshot, "area:living_room") == [OTHER]

    second = real_home.Home(home.config)
    second.clock.now = home.clock.now
    with real_home.worker_on_transport(second):
        real_home.run_on_clock(second.clock, lambda: real_home.lifetime(second, read_back))


def test_an_excluded_radar_cannot_be_learned_applied_or_set_recording(play):
    async def scenario(home):
        await _recording(home)
        await home.ws("set_excluded", device_id=RADAR, excluded=True)
        for command, fields in (
            ("start_learning", {}),
            ("learn", {}),
            ("apply", {}),
            ("set_recording", {"enabled": True}),
        ):
            with pytest.raises(WebsocketError, match="excluded"):
                await home.ws(command, device_id=RADAR, **fields)

    play(scenario)


def test_a_saved_zone_leaves_out_an_excluded_member_until_it_returns(play):
    async def scenario(home):
        await _recording(home)
        zone = await home.ws(
            "configure_room", name="Sofa corner", area_id="living_room", device_ids=[RADAR, OTHER]
        )
        group_id = zone["group_id"] if isinstance(zone, dict) and "group_id" in zone else None
        snapshot = await home.ws("snapshot")
        group_id = group_id or next(
            key
            for key, item in snapshot["rooms"]["groups"].items()
            if item["name"] == "Sofa corner"
        )
        await home.ws("set_excluded", device_id=RADAR, excluded=True)
        assert _members(await home.ws("snapshot"), group_id) == [OTHER]
        await home.ws("set_excluded", device_id=RADAR, excluded=False)
        assert _members(await home.ws("snapshot"), group_id) == [RADAR, OTHER]

    play(scenario)


def test_excluding_refuses_an_unknown_radar(play):
    async def scenario(home):
        await _recording(home)
        with pytest.raises(WebsocketError, match="Unknown"):
            await home.ws("set_excluded", device_id="no_such_radar", excluded=True)

    play(scenario)


def test_overnight_learning_leaves_out_an_excluded_radar(play):
    from tuner_under_test.runtime import schedule

    def learned(runtime):
        plan = schedule._plan(runtime, runtime.data["devices"].items())
        return {key for item in plan for key in (item[0], *item[2])}

    async def scenario(home):
        await _recording(home)
        assert RADAR in learned(home.runtime)
        await home.ws("set_excluded", device_id=RADAR, excluded=True)
        assert RADAR not in learned(home.runtime)
        assert OTHER in learned(home.runtime)

    play(scenario)


def test_a_room_result_that_includes_an_excluded_radar_is_not_applied(play):
    from tuner_under_test.rooms import application

    async def scenario(home):
        await _recording(home)
        await home.ws("set_excluded", device_id=RADAR, excluded=True)
        learned = {"thresholds": {RADAR: {"g1_move": 30}, OTHER: {"g1_move": 30}}}
        with pytest.raises(ValueError, match="excluded"):
            application.guard(home.runtime, learned, {})

    play(scenario)


def test_excluding_twice_or_including_an_included_radar_changes_nothing(play):
    from tuner_under_test.runtime import exclusion

    async def scenario(home):
        await _recording(home)
        assert await home.ws("set_excluded", device_id=RADAR, excluded=False) == {"excluded": False}
        await home.ws("set_excluded", device_id=RADAR, excluded=True)
        since = home.runtime.data["devices"][RADAR]["excluded"]["since"]
        home.clock.moved(60)
        await home.ws("set_excluded", device_id=RADAR, excluded=True)
        assert home.runtime.data["devices"][RADAR]["excluded"]["since"] == since
        with pytest.raises(ValueError, match="excluded or included"):
            exclusion.set_excluded(home.runtime, RADAR, "yes")

    play(scenario)
