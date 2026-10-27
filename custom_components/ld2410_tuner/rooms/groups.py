"""Area defaults and explicit coverage zones; never change Home Assistant areas."""

from uuid import uuid4

from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr

from . import sets


def recording_members(group, devices):
    """Members that record, including devices whose recording setting is absent."""
    return [
        key for key in group["device_ids"] if devices.get(key, {}).get("recording_enabled", True)
    ]


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
    _membership_changed(runtime, groups)
    runtime._schedule_save()
    return {"id": key, **saved}


def remove(runtime, group_id):
    before = definitions(runtime)[2]
    # Removing an area override restores its registry-derived membership.
    runtime.data.setdefault("room_groups", {}).pop(group_id, None)
    runtime.data.setdefault("room_assessments", {}).pop(group_id, None)
    runtime.data.setdefault("room_learning", {}).pop(group_id, None)
    if not group_id.startswith("area:"):
        # Restoring an area default keeps its labels; a zone's labels go with it.
        runtime.data.get("room_labels", {}).pop(group_id, None)
        runtime.data.get("room_label_revision", {}).pop(group_id, None)
        runtime.data.get("room_sensor_revision", {}).pop(group_id, None)
        _forget_sources(runtime, group_id)
    _membership_changed(runtime, before)
    runtime._schedule_save()
    return {"ok": True}


def _forget_sources(runtime, group_id):
    from . import room_sources

    room_sources.forget(runtime, group_id)


def _membership_changed(runtime, before):
    from . import membership

    membership.membership_changed(runtime, before)


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


PAUSED_MEMBER = (
    "Enable recording for every member, or remove paused radars from this learning group"
)


def _learnable(known, group_id):
    group = known.get(group_id)
    if not group:
        raise ValueError("This zone no longer exists")
    if group["kind"] == "area" and any(
        item["kind"] == "zone" and item["area_id"] == group["area_id"] for item in known.values()
    ):
        raise ValueError(
            "This area is split into zones. Learn its zones from their headers or linked band."
        )
    return group


def _paused(runtime, chosen):
    return any(
        not runtime.data["devices"].get(key, {}).get("recording_enabled", True)
        for group in chosen
        for key in group["device_ids"]
    )


def _set_id(found, key):
    """A set id, or the id of the set holding group ``key``; otherwise ``key`` itself."""
    return next(
        (set_id for set_id, item in found.items() if key == set_id or key in item["group_ids"]),
        key,
    )


def learning_set(runtime, key):
    """Resolve a set id, or one of its group ids, to ``(set_id, name, {group_id: group})``.

    A group in no learning set (for example a single-radar zone) learns as its own
    one-group set, as before learning sets existed."""
    known = definitions(runtime)[2]
    found = sets.learning_sets(known)
    set_id = _set_id(found, key)
    group_ids = found[set_id]["group_ids"] if set_id in found else [key]
    chosen = {group_id: _learnable(known, group_id) for group_id in group_ids}
    name = " + ".join(item["name"] for item in chosen.values())
    if _paused(runtime, chosen.values()):
        raise ValueError(f"{name}: {PAUSED_MEMBER}")
    return set_id, name, chosen


def current_sets(runtime):
    """The learning sets of the current group definitions (``sets.learning_sets``)."""
    return sets.learning_sets(definitions(runtime)[2])


def set_id_for(runtime, key):
    """The stored learning key for ``key`` (a set id or one of its group ids)."""
    return _set_id(current_sets(runtime), key)


def for_device(runtime, device_id):
    """Return the id of the device's learning set (a group id for a one-group set)."""
    return sets.set_for_device(current_sets(runtime), device_id)
