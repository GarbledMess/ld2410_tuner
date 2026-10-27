"""Bermuda room migration and websocket checks in a fresh disposable HA.

The synthetic radar and zone have no area; unknown legacy area text must be
retained for conflict recovery without activating a room. No Bermuda integration
is installed, so discovery returns an empty list and the saved phone is unavailable.
"""

import asyncio
import json
from copy import deepcopy

import aiohttp
from export_checks import (
    BASE,
    CONFIG,
    DOMAIN,
    STORAGE_KEY,
    STORAGE_MINOR_VERSION,
    STORAGE_VERSION,
    add_integration,
    command,
    onboard,
)

ENTITY = "sensor.lab_phone_area"
GROUP = "zone:probe"
AREA = "Lab unknown area"


def legacy_source(distance):
    return {
        "sources": [
            {"entity_id": ENTITY, "kind": "bermuda", "area": AREA, "max_distance": distance}
        ],
        "confidence": 80,
        "mark_not_present": False,
        "start_buffer_seconds": 0,
        "end_buffer_seconds": 0,
    }


def synthetic_store():
    return {
        "version": STORAGE_VERSION,
        "minor_version": STORAGE_MINOR_VERSION,
        "key": STORAGE_KEY,
        "data": {
            "devices": {
                "probe": {"training_state": "unknown", "presence_sources": legacy_source(3)}
            },
            "room_groups": {
                GROUP: {
                    "name": "Lab zone",
                    "kind": "zone",
                    "area_id": None,
                    "device_ids": ["probe"],
                }
            },
            "room_presence_sources": {GROUP: legacy_source(4)},
            "bermuda_migrated": True,  # Exercise the upgrade from the earlier migration.
        },
    }


def assert_migrated(snapshot):
    assert snapshot["bermuda"] == {"devices": [ENTITY], "max_distance": None, "rooms": {}}
    if "probe" in snapshot["devices"]:
        assert snapshot["devices"]["probe"]["presence_sources"]["sources"] == []
    assert snapshot["rooms"]["groups"][GROUP]["presence_sources"]["sources"] == []


async def ready_snapshot(socket):
    for ident in range(1, 120):
        reply = await command(socket, ident, {"type": f"{DOMAIN}/snapshot"})
        if reply.get("success"):
            return reply["result"]
        await asyncio.sleep(1)
    raise TimeoutError("the integration's runtime did not start")


async def check_commands(socket, snapshot):
    reply = await command(socket, 200, {"type": f"{DOMAIN}/bermuda_devices"})
    assert reply.get("success"), reply.get("error")
    assert reply["result"] == [], "No real Bermuda registry entities exist in this lab"
    settings = deepcopy(snapshot["bermuda"])
    settings["max_distance"] = 2.5
    reply = await command(
        socket, 201, {"type": f"{DOMAIN}/configure_bermuda", "settings": settings}
    )
    assert reply.get("success"), reply.get("error")
    assert reply["result"] == settings
    invalid_range = {**settings, "max_distance": 0}
    invalid_room = {**settings, "rooms": {GROUP: {"max_distance": None}}}
    for ident, invalid in ((202, invalid_range), (204, invalid_room)):
        reply = await command(
            socket, ident, {"type": f"{DOMAIN}/configure_bermuda", "settings": invalid}
        )
        assert not reply.get("success") and reply["error"]["code"] == "invalid_request", reply
        reply = await command(socket, ident + 1, {"type": f"{DOMAIN}/snapshot"})
        assert reply.get("success"), reply.get("error")
        assert reply["result"]["bermuda"] == settings, (
            "Rejected settings must leave valid settings intact"
        )
    reply = await command(socket, 206, {"type": f"{DOMAIN}/recording_days", "device_id": "probe"})
    assert reply.get("success"), reply.get("error")
    return {
        "migration": "radar and zone converted; unresolved areas retained for conflict recovery",
        "bermuda_devices": "ok (empty registry)",
        "configure_valid": "ok",
        "configure_invalid": "zero range and zone room rejected atomically",
        "snapshot": "ok",
        "recording_days": reply["result"],
    }


async def main():
    async with aiohttp.ClientSession() as session:
        token = await onboard(session)
        # Only the fresh disposable container's synthetic store is written here.
        (CONFIG / ".storage").mkdir(exist_ok=True)
        (CONFIG / ".storage" / STORAGE_KEY).write_text(
            json.dumps(synthetic_store()), encoding="utf-8", newline="\n"
        )
        await add_integration(session, {"Authorization": "Bearer " + token})
        async with session.ws_connect(BASE + "/api/websocket") as socket:
            await socket.receive_json()
            await socket.send_json({"type": "auth", "access_token": token})
            assert (await socket.receive_json())["type"] == "auth_ok"
            snapshot = await ready_snapshot(socket)
            assert_migrated(snapshot)
            evidence = await check_commands(socket, snapshot)
            print("PASS: " + json.dumps(evidence), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
