"""One period labelled on several rooms, zones and radars at once, the selection remembered
and named.

Each item is labelled at its own level, exactly as its own chart labels it: a room or zone
through ``room_labels.label_room_range`` (its Empty is Empty for each of its radars; its
Present is credited to the radar that saw it), a radar through
``labels.label_history_range`` ("this radar should have detected here"). Every item of a
request is checked before any is labelled, so a rejected request labels nothing. The last
selection and the named selections are kept with the integration's data, so every browser
sees them; radars excluded and rooms gone since are left out when they are read back.
"""

from __future__ import annotations

from ..history import labels
from ..rooms import groups, room_labels
from . import exclusion

KEY = "label_selections"
NAME_LIMIT = 60


def _store(runtime):
    return runtime.data.setdefault(KEY, {"last": None, "templates": {}})


def _chosen(runtime, device_ids, group_ids):
    """``{"device_ids", "group_ids"}`` once each and sorted; every radar known and included,
    every room or zone one that exists."""
    chosen = {"device_ids": sorted(set(device_ids)), "group_ids": sorted(set(group_ids))}
    if not chosen["device_ids"] and not chosen["group_ids"]:
        raise ValueError("Choose at least one room, zone or radar")
    devices = runtime.data.get("devices", {})
    for device_id in chosen["device_ids"]:
        if device_id not in devices:
            raise ValueError("Unknown device")
        exclusion.require_included(runtime, device_id)
    definitions = groups.definitions(runtime)[2]
    if any(group_id not in definitions for group_id in chosen["group_ids"]):
        raise ValueError("This room or zone no longer exists; refresh the panel")
    return chosen


async def label_several(runtime, device_ids, group_ids, start, end, state):
    """Label ``[start, end)`` ``state`` on every room, zone and radar chosen."""
    chosen = _chosen(runtime, device_ids, group_ids)
    labels.validate_history_range(start, end, state)
    for group_id in chosen["group_ids"]:
        room_labels.label_room_range(runtime, group_id, start, end, state)
    for device_id in chosen["device_ids"]:
        await labels.label_history_range(runtime, device_id, start, end, state)
    _store(runtime)["last"] = chosen
    runtime._schedule_save()
    return {"ok": True, "labelled": chosen}


def _name(name):
    name = " ".join(str(name).split())
    if not name:
        raise ValueError("Give the selection a name")
    if len(name) > NAME_LIMIT:
        raise ValueError(f"Keep the name under {NAME_LIMIT} characters")
    return name


def save_template(runtime, name, device_ids, group_ids):
    """Save (or replace) the chosen rooms, zones and radars as ``name``."""
    name, chosen = _name(name), _chosen(runtime, device_ids, group_ids)
    _store(runtime)["templates"][name] = chosen
    runtime._schedule_save()
    return {"ok": True}


def delete_template(runtime, name):
    templates = _store(runtime)["templates"]
    if name not in templates:
        raise ValueError("No saved selection has that name")
    del templates[name]
    runtime._schedule_save()
    return {"ok": True}


def listed(runtime):
    """``{"last", "templates"}`` for the panel, without radars excluded or rooms gone since."""
    saved = runtime.data.get(KEY) or {}
    devices = runtime.data.get("devices", {})
    definitions = groups.definitions(runtime)[2]

    def current(chosen):
        return {
            "device_ids": [
                key
                for key in chosen.get("device_ids", ())
                if key in devices and not exclusion.is_excluded(devices[key])
            ],
            "group_ids": [key for key in chosen.get("group_ids", ()) if key in definitions],
        }

    last = saved.get("last")
    templates = sorted(saved.get("templates", {}).items(), key=lambda item: item[0].lower())
    return {
        "last": None if last is None else current(last),
        "templates": [{"name": name, **current(chosen)} for name, chosen in templates],
    }
