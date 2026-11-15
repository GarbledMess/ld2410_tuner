"""Labelling one period on several rooms, zones and radars at once, as the panel's own card
does: each item is labelled at its own level, the last selection is remembered and
selections can be saved under a name."""

import pytest
import real_home  # noqa: F401
from real_home import WebsocketError
from real_shape import DAY, TODAY

LOW, HIGH = TODAY - DAY + 1234.5, TODAY - DAY + 1834.25
RECORDING = ["hall_1", "kitchen_1", "kitchen_2", "living_room_1", "living_room_2", "garage_1"]
ROOMS = ["area:kitchen", "area:living_room"]
RADARS = ["hall_1"]


async def _recording(home):
    for radar in RECORDING:
        await home.ws("set_recording", device_id=radar, enabled=True)


def _periods(labels):
    return [
        (label["start"], label["end"], label["state"])
        for label in labels
        if label["start"] < HIGH
        and label["end"] > LOW
        and label.get("source", "manual") == "manual"
    ]


def _radar(home, radar):
    return _periods(home.runtime.data["devices"][radar].get("history_labels", []))


def _room(home, group_id):
    return _periods(home.runtime.data.get("room_labels", {}).get(group_id, []))


def _covers(periods, state):
    return any(s <= LOW and e >= HIGH and found == state for s, e, found in periods)


def _everything(home):
    return (
        {radar: _radar(home, radar) for radar in home.runtime.data["devices"]},
        {key: _room(home, key) for key in home.runtime.data.get("room_labels", {})},
    )


async def _label(home, state="not_present", **fields):
    fields.setdefault("device_ids", RADARS)
    fields.setdefault("group_ids", ROOMS)
    return await home.ws("label_several", start=LOW, end=HIGH, state=state, **fields)


def test_each_item_is_labelled_at_its_own_level_and_nothing_else(play):
    async def scenario(home):
        await _recording(home)
        radars, rooms = _everything(home)
        assert not any(_covers(_room(home, key), "not_present") for key in ROOMS)
        assert not _covers(_radar(home, "hall_1"), "not_present")

        found = await _label(home)
        assert found["labelled"] == {"device_ids": RADARS, "group_ids": ROOMS}
        for key in ROOMS:
            assert _covers(_room(home, key), "not_present")
        assert _covers(_radar(home, "hall_1"), "not_present")
        # A room's label covers its radars as a room label: their own labels are untouched.
        after_radars, after_rooms = _everything(home)
        assert {k: v for k, v in after_radars.items() if k != "hall_1"} == {
            k: v for k, v in radars.items() if k != "hall_1"
        }
        assert {k: v for k, v in after_rooms.items() if k not in ROOMS} == {
            k: v for k, v in rooms.items() if k not in ROOMS
        }

    play(scenario)


def test_a_zone_is_labelled_as_a_zone(play):
    async def scenario(home):
        await _recording(home)
        zone = (
            await home.ws(
                "configure_room", name="Sofa", area_id="living_room", device_ids=["living_room_1"]
            )
        )["id"]
        await _label(home, state="present", device_ids=[], group_ids=[zone])
        assert _covers(_room(home, zone), "present")
        assert not _covers(_room(home, "area:living_room"), "present")

    play(scenario)


def test_the_last_selection_is_remembered_and_none_before_the_first(play):
    async def scenario(home):
        await _recording(home)
        assert (await home.ws("snapshot"))["label_selections"] == {"last": None, "templates": []}
        await _label(home, device_ids=["hall_1", "hall_1"], group_ids=list(reversed(ROOMS)))
        last = (await home.ws("snapshot"))["label_selections"]["last"]
        assert last == {"device_ids": ["hall_1"], "group_ids": ROOMS}

    play(scenario)


def test_a_rejected_request_labels_nothing(play):
    async def scenario(home):
        await _recording(home)
        await home.ws("set_excluded", device_id="garage_1", excluded=True)
        before = _everything(home)
        for fields in (
            {"device_ids": [], "group_ids": []},
            {"device_ids": ["hall_1", "no_such_radar"]},
            {"device_ids": ["hall_1", "garage_1"]},
            {"group_ids": ["area:kitchen", "area:no_such_room"]},
        ):
            with pytest.raises(WebsocketError):
                await _label(home, **fields)
        with pytest.raises(WebsocketError):
            await home.ws(
                "label_several",
                device_ids=RADARS,
                group_ids=ROOMS,
                start=HIGH,
                end=LOW,
                state="present",
            )
        assert _everything(home) == before
        assert (await home.ws("snapshot"))["label_selections"]["last"] is None

    play(scenario)


def test_named_selections_are_saved_replaced_and_deleted(play):
    async def scenario(home):
        await _recording(home)
        await home.ws(
            "save_label_template", name=" Downstairs ", device_ids=RADARS, group_ids=ROOMS
        )
        await home.ws("save_label_template", name="hall", device_ids=["hall_1"], group_ids=[])
        templates = (await home.ws("snapshot"))["label_selections"]["templates"]
        assert templates == [
            {"name": "Downstairs", "device_ids": RADARS, "group_ids": ROOMS},
            {"name": "hall", "device_ids": ["hall_1"], "group_ids": []},
        ]
        await home.ws(
            "save_label_template", name="Downstairs", device_ids=[], group_ids=["area:kitchen"]
        )
        await home.ws("delete_label_template", name="hall")
        templates = (await home.ws("snapshot"))["label_selections"]["templates"]
        assert templates == [
            {"name": "Downstairs", "device_ids": [], "group_ids": ["area:kitchen"]}
        ]

    play(scenario)


def test_a_template_needs_a_name_and_known_items(play):
    async def scenario(home):
        await _recording(home)
        for fields in (
            {"name": "  ", "device_ids": ["hall_1"], "group_ids": []},
            {"name": "Hall", "device_ids": [], "group_ids": []},
            {"name": "Hall", "device_ids": ["no_such_radar"], "group_ids": []},
            {"name": "Hall", "device_ids": [], "group_ids": ["area:no_such_room"]},
        ):
            with pytest.raises(WebsocketError):
                await home.ws("save_label_template", **fields)
        with pytest.raises(WebsocketError):
            await home.ws("delete_label_template", name="Never saved")
        assert (await home.ws("snapshot"))["label_selections"]["templates"] == []

    play(scenario)


def test_selections_leave_out_radars_excluded_and_rooms_gone_since(play):
    async def scenario(home):
        await _recording(home)
        await _label(home, device_ids=["hall_1", "garage_1"], group_ids=ROOMS)
        await home.ws("save_label_template", name="Both", device_ids=["garage_1"], group_ids=ROOMS)
        await home.ws("set_excluded", device_id="garage_1", excluded=True)
        for radar in ("kitchen_1", "kitchen_2"):  # The kitchen has no recording radar left.
            await home.ws("set_recording", device_id=radar, enabled=False)
        selections = (await home.ws("snapshot"))["label_selections"]
        assert selections["last"] == {"device_ids": ["hall_1"], "group_ids": ["area:living_room"]}
        assert selections["templates"] == [
            {"name": "Both", "device_ids": [], "group_ids": ["area:living_room"]}
        ]

    play(scenario)
