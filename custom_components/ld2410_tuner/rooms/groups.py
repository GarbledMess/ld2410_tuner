"""Area defaults and explicit coverage zones; never change Home Assistant areas."""

from uuid import uuid4

from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr


def inventory(runtime):
    devices = dr.async_get(runtime.hass)
    areas = {area.id: area.name for area in ar.async_get(runtime.hass).async_list_areas()}
    members = {}
    for key, item in runtime.data["devices"].items():
        if not item.get("entities"):
            continue
        device = devices.async_get(key)
        members[key] = {
            "name": (device.name_by_user or device.name or key) if device else key,
            "area_id": device.area_id if device else None,
            "recording_enabled": item.get("recording_enabled", True),
        }
    return areas, members


def definitions(runtime):
    areas, members = inventory(runtime)
    groups = {}
    for area_id, name in areas.items():
        ids = sorted(
            key
            for key, item in members.items()
            if item["area_id"] == area_id and item["recording_enabled"]
        )
        if ids:
            groups[f"area:{area_id}"] = {
                "name": name,
                "area_id": area_id,
                "kind": "area",
                "device_ids": ids,
                "automatic": True,
            }
    for key, saved in runtime.data.get("room_groups", {}).items():
        groups[key] = {**saved, "automatic": False}
    return areas, members, groups


def configure(runtime, group_id, name, area_id, device_ids):
    areas, members, groups = definitions(runtime)
    _validate(name, area_id, device_ids, areas, members)
    if group_id is not None and group_id not in groups:
        raise ValueError("This group no longer exists; refresh the panel")
    key = group_id or f"zone:{uuid4().hex}"
    kind = groups[key]["kind"] if key in groups else "zone"
    if kind == "area" and area_id != groups[key]["area_id"]:
        raise ValueError("An area group must keep its original area")
    saved = {
        "name": name.strip(),
        "area_id": area_id,
        "kind": kind,
        "device_ids": sorted(set(device_ids)),
    }
    runtime.data.setdefault("room_groups", {})[key] = saved
    runtime.data.setdefault("room_assessments", {}).pop(key, None)
    runtime._schedule_save()
    return {"id": key, **saved}


def remove(runtime, group_id):
    # Removing an area override restores its registry-derived membership.
    runtime.data.setdefault("room_groups", {}).pop(group_id, None)
    runtime.data.setdefault("room_assessments", {}).pop(group_id, None)
    runtime.data.setdefault("room_learning", {}).pop(group_id, None)
    runtime._schedule_save()
    return {"ok": True}


def _validate(name, area_id, device_ids, areas, members):
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
        raise ValueError("Choose a name of 1 to 80 characters")
    if area_id is not None and area_id not in areas:
        raise ValueError("Choose an existing Home Assistant area, or no area")
    if (
        not isinstance(device_ids, list)
        or not device_ids
        or any(key not in members for key in device_ids)
    ):
        raise ValueError("Choose at least one discovered radar")


def learning_group(runtime, group_id):
    _, _, known = definitions(runtime)
    group = known.get(group_id)
    if not group:
        raise ValueError("This zone no longer exists")
    if group["kind"] == "area" and any(
        item["kind"] == "zone" and item["area_id"] == group["area_id"] for item in known.values()
    ):
        raise ValueError("This area contains independent zones. Learn each zone separately.")
    if any(
        not runtime.data["devices"].get(key, {}).get("recording_enabled", True)
        for key in group["device_ids"]
    ):
        raise ValueError(
            "Enable recording for every member, or remove paused radars from this learning group"
        )
    return group


def for_device(runtime, device_id):
    _, _, known = definitions(runtime)
    zones = [
        key
        for key, item in known.items()
        if item["kind"] == "zone" and device_id in item["device_ids"]
    ]
    if len(zones) > 1:
        raise ValueError(
            "This radar belongs to multiple zones. Choose Learn together on the intended zone."
        )
    if zones:
        key = zones[0]
        return key if len(known[key]["device_ids"]) > 1 else None
    areas = [
        key
        for key, item in known.items()
        if item["kind"] == "area" and device_id in item["device_ids"]
    ]
    if len(areas) > 1:
        raise ValueError("This radar belongs to multiple area groups. Choose the intended group.")
    return _area_learning_group(known, areas)


def _area_learning_group(known, areas):
    if not areas:
        return None
    key = areas[0]
    parent = known[key]
    subdivided = any(
        item["kind"] == "zone" and item["area_id"] == parent["area_id"] for item in known.values()
    )
    return key if len(parent["device_ids"]) > 1 and not subdivided else None
