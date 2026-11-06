"""What the panel shows and exports on the real-shaped home: Bermuda rooms and a radar's export."""

import base64
import datetime
import json
import types

import pytest
import real_shape
from real_home import WebsocketError
from real_shape import DAY, TODAY
from tuner_under_test.history import dayfile


def test_bermuda_lists_phones_and_explains_every_room_it_cannot_use(play):
    async def scenario(home):
        snapshot = await home.ws("snapshot")
        phones = {item["entity_id"]: item for item in snapshot["bermuda_devices"]}
        assert set(phones) == set(real_shape.PHONES)
        assert phones["sensor.ana_phone_area"]["name"] == "Ana's phone"
        assert phones["sensor.ana_phone_area"]["area_id"] == "kitchen"
        missing = {room["area_id"]: room["reason"] for room in snapshot["bermuda_missing_rooms"]}
        assert missing == {"patio": "no_radar", "garage": "recording_off"}

        # A room it cannot use is refused; one it can is accepted and survives the snapshot.
        settings = {"devices": sorted(real_shape.PHONES), "max_distance": 6}
        with pytest.raises(WebsocketError, match="area room"):
            await home.ws(
                "configure_bermuda",
                settings={**settings, "rooms": {"area:patio": {"max_distance": None}}},
            )
        rooms = {"area:office": {"max_distance": 3}}
        saved = await home.ws("configure_bermuda", settings={**settings, "rooms": rooms})
        assert saved["rooms"] == rooms
        assert (await home.ws("bermuda_devices"))[0]["entity_id"] == "sensor.ana_phone_area"

        # Switching the garage's recording on makes the room usable.
        await home.ws("set_recording", device_id="garage_1", enabled=True)
        missing = (await home.ws("snapshot"))["bermuda_missing_rooms"]
        assert [room["area_id"] for room in missing] == ["patio"]

    play(scenario)


def test_a_phone_in_a_room_writes_automatic_room_labels_with_real_edges(play):
    async def scenario(home):
        await home.ws(
            "configure_bermuda",
            settings={
                "devices": ["sensor.ana_phone_area"],
                "max_distance": None,
                "rooms": {"area:office": {"max_distance": None}},
            },
        )
        data = home.runtime.data
        before = len(data["room_labels"]["area:office"])
        home.hass.values["sensor.ana_phone_area"] = types.SimpleNamespace(
            state="Office",
            attributes={"friendly_name": "Ana's phone", "area_name": "Office", "area_id": "office"},
        )
        home.hass.values["sensor.ana_phone_area_range"] = types.SimpleNamespace(
            state="1.5", attributes={}
        )
        entered = home.clock()
        for _ in range(40):
            home.clock.moved(1.7)  # The sensor's edges are wherever the poll fell.
            await home.poll()
        labels = data["room_labels"]["area:office"]
        assert len(labels) > before
        newest = labels[-1]
        assert newest["source"] == "sensor"
        assert newest["state"] == "present"
        assert entered <= newest["start"] <= entered + 30
        assert newest["end"] > newest["start"]

    play(scenario)


def test_export_downloads_every_recorded_day_and_the_open_one_losslessly(play):
    async def scenario(home):
        radar = "office_1"
        exported = await home.ws("export_file", device_id=radar)
        assert exported["filename"] == "ld2410-tuner-office_1.json"
        assert "authSig=" in exported["url"]
        token = exported["url"].split("/")[-1].split("?")[0]
        path = home.config / real_shape.STORE_FOLDER / real_shape.DOMAIN / "exports"
        data = json.loads((path / f"{token}.json").read_text(encoding="utf8"))
        device = data["devices"][radar]
        assert device["name"] == "Office 1"
        assert device["area_id"] == "office"
        files = device["history"]["day_files"]
        assert len(files) == real_shape.RETAINED_DAYS + 1
        today = datetime.datetime.fromtimestamp(TODAY, datetime.UTC).date().isoformat()
        live = dayfile.decode_day(base64.b64decode(files[today]))
        assert len(live.times) == real_shape.rows_today(radar)
        past = datetime.datetime.fromtimestamp(TODAY - 9 * DAY, datetime.UTC).date().isoformat()
        day = dayfile.decode_day(base64.b64decode(files[past]))
        assert len(day.times) == real_shape.rows_on(radar, 9)
        assert exported["bytes"] > 1024 * 1024

        # One export is kept: the next replaces it and its file is gone.
        second = await home.ws("export_file", device_id="hall_1")
        assert second["filename"] == "ld2410-tuner-hall_1.json"
        assert not (path / f"{token}.json").exists()

    play(scenario)


def test_every_radar_and_room_appears_in_the_snapshot_the_panel_is_drawn_from(play):
    async def scenario(home):
        snapshot = await home.ws("snapshot")
        assert set(snapshot["devices"]) == set(real_shape.radars())
        rooms = snapshot["rooms"]["groups"]
        assert set(rooms) >= {f"area:{room}" for room in real_shape.ROOMS} - {"area:garage"}
        assert sorted(rooms["area:kitchen"]["device_ids"]) == ["kitchen_1", "kitchen_2"]
        assert snapshot["devices"]["kitchen_2"]["current_thresholds"]["g0_move"] == 50
        json.dumps(snapshot)  # A websocket result: all of it must be JSON.

    play(scenario)
