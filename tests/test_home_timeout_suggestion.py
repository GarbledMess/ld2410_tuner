"""Learn suggests each radar's timeout on the real-shaped home: every Learn scores the timeouts
at the radar's live thresholds, and sets the cheaper one on the radar only where automatic
timeouts are on (globally or for that radar)."""

import types

import pytest
import real_home  # noqa: F401
from real_home import WebsocketError
from real_shape import DAY, TODAY
from tuner_under_test.calibration import timeout_apply
from tuner_under_test.rooms import groups

RADAR = "hall_1"


def _timeout_entity(home, radar, state="1.0", unit="s"):
    entity_id = f"number.{radar}_timeout"
    home.hass.entity_registry.add(entity_id, radar)
    home.hass.values[entity_id] = types.SimpleNamespace(
        state=state, attributes={"min": 0, "max": 65535, "step": 1, "unit_of_measurement": unit}
    )
    return entity_id


async def _labelled(home, radar):
    start = TODAY - DAY
    await home.ws(
        "label_history", device_id=radar, start=start, end=start + 3 * 3600, state="present"
    )
    await home.ws(
        "label_history",
        device_id=radar,
        start=start + 4 * 3600,
        end=start + 7 * 3600,
        state="not_present",
    )


def test_learn_suggests_a_timeout_and_leaves_the_radar_alone_by_default(play):
    async def scenario(home):
        entity_id = _timeout_entity(home, RADAR)
        await _labelled(home, RADAR)
        _job, report = await home.learn(RADAR)
        found = report["timeouts"][RADAR]
        suggestion = found["suggestion"]
        assert found["current_timeout"] == 1.0
        assert {row["timeout"] for row in suggestion["candidates"]} == {
            1.0,
            2.0,
            3.0,
            5.0,
            8.0,
            12.0,
            15.0,
            20.0,
            30.0,
        }
        assert found["status"] in ("suggested", "kept")
        assert home.hass.values[entity_id].state == "1.0"
        shown = await home.ws("snapshot")
        assert shown["devices"][RADAR]["timeout_suggestion"]["suggestion"] == suggestion
        assert shown["devices"][RADAR]["timeout_apply_policy"]["effective"] == "off"

    play(scenario)


def test_with_automatic_timeouts_on_learn_sets_the_cheaper_timeout(play):
    async def scenario(home):
        # A two-minute timeout holds every false detection two minutes: never the cheapest.
        entity_id = _timeout_entity(home, RADAR, state="120.0")
        await _labelled(home, RADAR)
        await home.ws("configure_device_timeout_apply", device_id=RADAR, mode="all")
        _job, report = await home.learn(RADAR)
        found = report["timeouts"][RADAR]
        assert found["status"] == "applied"
        assert found["previous_timeout"] == 120.0
        assert found["applied_timeout"] == found["suggestion"]["timeout"] < 120.0
        assert float(home.hass.values[entity_id].state) == found["applied_timeout"]

    play(scenario)


def test_the_global_default_turns_automatic_timeouts_on_for_every_radar(play):
    async def scenario(home):
        await home.ws(
            "configure_learning_schedule",
            enabled=False,
            at="03:00",
            timeout_apply=True,
            timeout_apply_scope="all",
        )
        shown = await home.ws("snapshot")
        assert shown["learning_schedule"]["timeout_apply"] is True
        assert shown["devices"][RADAR]["timeout_apply_policy"]["effective"] == "all"
        policy = await home.ws("configure_device_timeout_apply", device_id=RADAR, mode="off")
        assert policy == {"override": "off", "global": "all", "effective": "off"}

    play(scenario)


def test_overnight_only_does_not_set_a_timeout_after_a_manual_learn(play):
    async def scenario(home):
        entity_id = _timeout_entity(home, RADAR, state="120.0")
        await _labelled(home, RADAR)
        await home.ws("configure_device_timeout_apply", device_id=RADAR, mode="overnight")
        _job, report = await home.learn(RADAR)
        assert report["timeouts"][RADAR]["status"] == "suggested"
        assert home.hass.values[entity_id].state == "120.0"

    play(scenario)


def test_a_timeout_in_milliseconds_is_written_in_milliseconds(play):
    async def scenario(home):
        entity_id = _timeout_entity(home, RADAR, state="1000", unit="ms")
        await timeout_apply.write(home.runtime, RADAR, entity_id, 3.0)
        assert home.hass.writes[-1] == (
            "number",
            "set_value",
            {"entity_id": entity_id, "value": 3000.0},
        )

    play(scenario)


def test_a_timeout_the_radar_cannot_take_is_not_written(play):
    async def scenario(home):
        entity_id = _timeout_entity(home, RADAR)
        home.hass.values[entity_id].attributes["max"] = 2
        with pytest.raises(ValueError):
            await timeout_apply.write(home.runtime, RADAR, entity_id, 3.0)
        assert home.hass.values[entity_id].state == "1.0"

    play(scenario)


def test_a_radar_without_labelled_presence_gets_no_suggestion(play):
    async def scenario(home):
        device = home.runtime.data["devices"]["garage_1"]
        device["history_labels"] = []
        device.pop("group_labels", None)
        home.runtime.data.get("room_labels", {}).pop("area:garage", None)
        _job, report = await home.learn("garage_1")
        found = report["timeouts"]["garage_1"]
        assert found["suggestion"] is None
        assert found["status"] == "unavailable"

    play(scenario)


def test_an_unknown_radar_is_refused(play):
    async def scenario(home):
        with pytest.raises(WebsocketError):
            await home.ws("configure_device_timeout_apply", device_id="no_such_radar", mode="all")

    play(scenario)


def test_a_room_learn_suggests_a_timeout_for_every_radar_in_the_room(play):
    async def scenario(home):
        _job, report = await home.learn("kitchen_1")
        members = sorted(
            home.runtime.data["room_learning"]["area:kitchen"]["slots"]["user"]["thresholds"]
        )
        assert len(members) == 2
        assert sorted(report["timeouts"]) == members
        for radar in members:
            assert (
                home.runtime.data["devices"][radar]["timeout_suggestion"]
                == (report["timeouts"][radar])
            )

    play(scenario)


def test_overnight_learning_sets_the_timeout_under_the_overnight_default(play):
    from tuner_under_test.runtime import schedule

    async def scenario(home):
        entity_id = _timeout_entity(home, RADAR, state="120.0")
        await _labelled(home, RADAR)
        await home.ws(
            "configure_learning_schedule",
            enabled=True,
            at="03:00",
            # Thresholds stay as they are: the timeout is scored at the radar's live ones.
            auto_apply=False,
            timeout_apply=True,
            timeout_apply_scope="overnight",
        )
        device = home.runtime.data["devices"][RADAR]
        await schedule._learn_device(home.runtime, RADAR, device, "2026-11-17")
        found = device["nightly_learning"]["timeouts"][RADAR]
        assert found["status"] == "applied", found
        assert found["source"] == "automatic"
        assert float(home.hass.values[entity_id].state) == found["applied_timeout"]

    play(scenario)


def test_a_timeout_home_assistant_only_reports_is_not_set(play):
    async def scenario(home):
        entity_id = "sensor.hall_1_timeout"
        home.hass.entity_registry.add(entity_id, RADAR)
        home.hass.values[entity_id] = types.SimpleNamespace(
            state="120", attributes={"unit_of_measurement": "s"}
        )
        await _labelled(home, RADAR)
        await home.ws("configure_device_timeout_apply", device_id=RADAR, mode="all")
        _job, report = await home.learn(RADAR)
        found = report["timeouts"][RADAR]
        assert found["status"] == "suggested"
        assert "cannot be set" in found["reason"]
        assert not [write for write in home.hass.writes if write[2].get("entity_id") == entity_id]

    play(scenario)


def test_a_radar_that_does_not_report_the_new_timeout_is_an_error(play):
    async def scenario(home):
        entity_id = _timeout_entity(home, RADAR, state="120.0")
        await _labelled(home, RADAR)
        await home.ws("configure_device_timeout_apply", device_id=RADAR, mode="all")
        services = home.hass.services

        async def ignored(domain, service, data, **kwargs):
            if data.get("entity_id") != entity_id:
                await home.hass._service_call(domain, service, data, **kwargs)

        home.hass.services = types.SimpleNamespace(async_call=ignored)
        try:
            _job, report = await home.learn(RADAR)
        finally:
            home.hass.services = services
        found = report["timeouts"][RADAR]
        assert found["status"] == "error"
        assert "device reports 120" in found["reason"]
        assert report["status"] == "completed"

    play(scenario)


def test_keeping_all_of_the_improvement_sets_the_cheapest_timeout(play):
    async def scenario(home):
        _timeout_entity(home, RADAR, state="120.0")
        await _labelled(home, RADAR)
        await home.ws("configure_learning_schedule", enabled=False, at="03:00", timeout_keep=100)
        shown = await home.ws("snapshot")
        assert shown["learning_schedule"]["timeout_keep"] == 100
        _job, report = await home.learn(RADAR)
        suggestion = report["timeouts"][RADAR]["suggestion"]
        assert suggestion["keep"] == 1.0
        rows = suggestion["candidates"]
        cheapest = min(rows, key=lambda row: (row["cost"], row["timeout"]))
        # The cheapest, unless it saves too little over the shortest to be more than noise.
        noise = rows[0]["cost"] - cheapest["cost"] < 0.03 * rows[0]["cost"]
        assert suggestion["timeout"] == (rows[0] if noise else cheapest)["timeout"]

    play(scenario)


def test_the_share_of_the_improvement_to_keep_is_at_least_half(play):
    async def scenario(home):
        with pytest.raises(WebsocketError):
            await home.ws("configure_learning_schedule", enabled=False, at="03:00", timeout_keep=40)
        shown = await home.ws("snapshot")
        assert shown["learning_schedule"]["timeout_keep"] == 80

    play(scenario)


def test_a_shared_room_s_labels_give_each_of_its_radars_a_timeout(play):
    async def scenario(home):
        start = TODAY - DAY
        for low, high, state in (
            (start, start + 3 * 3600, "present"),
            (start + 4 * 3600, start + 7 * 3600, "not_present"),
        ):
            await home.ws(
                "label_room_range", group_id="area:kitchen", start=low, end=high, state=state
            )
        members = groups.definitions(home.runtime)[2]["area:kitchen"]["device_ids"]
        assert len(members) == 2
        for radar in members:
            home.runtime.data["devices"][radar]["history_labels"] = []
        _job, report = await home.learn(members[0])
        for radar in members:
            suggestion = report["timeouts"][radar]["suggestion"]
            assert suggestion is not None, radar
            assert {row["timeout"] for row in suggestion["candidates"]} >= {1.0, 30.0}

    play(scenario)


def test_a_failed_timeout_check_is_an_error_for_each_radar_and_the_learn_completes(play):
    async def scenario(home):
        await _labelled(home, RADAR)
        calculate = timeout_apply.bridge.calculate

        async def failing(runtime, operation, prepare):
            if operation == "timeout_suggestion":
                raise RuntimeError("worker stopped")
            return await calculate(runtime, operation, prepare)

        timeout_apply.bridge.calculate = failing
        try:
            _job, report = await home.learn(RADAR)
        finally:
            timeout_apply.bridge.calculate = calculate
        assert report["timeouts"][RADAR] | {"at": 0} == {
            "at": 0,
            "source": "user",
            "status": "error",
            "reason": "worker stopped",
        }
        assert report["status"] == "completed"

    play(scenario)


def test_a_radar_whose_timeout_cannot_be_read_is_an_error(play):
    async def scenario(home):
        await _labelled(home, RADAR)
        read_timing = timeout_apply.read_timing

        def unreadable(runtime, device_id):
            if device_id == RADAR:
                raise KeyError(device_id)
            return read_timing(runtime, device_id)

        timeout_apply.read_timing = unreadable
        try:
            _job, report = await home.learn(RADAR)
        finally:
            timeout_apply.read_timing = read_timing
        found = report["timeouts"][RADAR]
        assert found["status"] == "error"
        assert found["reason"] == "Could not read this radar's timeout or thresholds."

    play(scenario)


def test_finding_a_room_s_radars_copies_each_input_once(play, monkeypatch):
    """Finding the radars of each room copies their room inputs once for the whole Learn, as
    the prepared views do, not once for every radar learned (twelve radars: 144 copies)."""
    from tuner_under_test.rooms import attribution

    async def scenario(home):
        start = TODAY - DAY
        await home.ws(
            "label_room_range",
            group_id="area:kitchen",
            start=start,
            end=start + 3 * 3600,
            state="present",
        )
        members = groups.definitions(home.runtime)[2]["area:kitchen"]["device_ids"]
        assert len(members) == 2
        copies = {}
        original = attribution.deepcopy

        def counted(value):
            copies[id(value)] = copies.get(id(value), 0) + 1
            return original(value)

        monkeypatch.setattr(attribution, "deepcopy", counted)
        await timeout_apply._suggestions(home.runtime, members)
        assert copies
        # Once to find the rooms' radars, once for the prepared views.
        assert max(copies.values()) <= 2, copies

    play(scenario)
