"""Removing a retired radar for good, so a replaced sensor's history cannot pollute its rooms.

Room truth, attribution, assessments and joint Learn read every member's recorded history.
A radar that was replaced keeps its old readings and labels until they are deleted, and a
saved zone or area override keeps listing it even after its entities leave Home Assistant.
Removing it deletes everything the tuner stored for it (recorded history, labels, histograms,
references and results) and takes it out of every saved zone, area override, label selection,
room assessment and joint result. Room labels are kept: they describe the room, so the
radars that replace it train on them.

Only a radar that is excluded or no longer in Home Assistant can be removed, so a radar in
use is never deleted by mistake. One still in Home Assistant stays excluded, with nothing
stored, so it is not rediscovered; one gone from Home Assistant is forgotten entirely and
would come back as a new, paused radar.
"""

import time

from homeassistant.helpers import entity_registry as er

from ..history import labels
from ..rooms import groups, membership
from . import bulk_labels, exclusion

IN_USE = "Exclude this radar first, or remove it from Home Assistant"
BUSY = "This radar is being learned or applied; try again when that finishes"


def remove_radar(runtime, device_id):
    devices = runtime.data["devices"]
    device = devices.get(device_id)
    if device is None:
        raise ValueError("Unknown device")
    in_home_assistant = _in_home_assistant(runtime, device_id)
    if in_home_assistant and not exclusion.is_excluded(device):
        raise ValueError(IN_USE)
    if device_id in runtime._learning_jobs or device_id in runtime._applying:
        raise ValueError(BUSY)
    _refuse_emptying_zones(runtime, device_id)
    before = groups.definitions(runtime)[2]
    labels.clear_samples(runtime, device_id)
    _forget_timeout(runtime, device_id)
    touched = _leave_saved_groups(runtime, device_id)
    touched |= {key for key, item in before.items() if device_id in item["device_ids"]}
    _forget_room_results(runtime, device_id, touched)
    _leave_selections(runtime, device_id)
    if in_home_assistant:
        devices[device_id] = {
            "recording_enabled": False,
            "training_state": "unknown",
            "entities": {},
            exclusion.KEY: {"since": time.time(), "recording": False},
        }
    else:
        devices.pop(device_id)
    runtime.refresh_devices(er.async_get(runtime.hass))
    membership.membership_changed(runtime, before)
    runtime._schedule_save()
    return {"ok": True}


def _in_home_assistant(runtime, device_id):
    """Whether Home Assistant still has this radar's LD2410 entities (an excluded radar's
    stored ``entities`` are emptied, so the registry is what tells)."""
    from .. import entity_index

    return device_id in entity_index.read(runtime, er.async_get(runtime.hass)).radars


def _refuse_emptying_zones(runtime, device_id):
    """A zone whose last radar this is would vanish with its labels; ask for its replacement
    to be added first. An area override just falls back to the area's radars."""
    devices = runtime.data["devices"]
    for key, saved in runtime.data.get("room_groups", {}).items():
        if key.startswith("area:") or device_id not in saved["device_ids"]:
            continue
        others = [
            item
            for item in saved["device_ids"]
            if item != device_id and item in devices and not exclusion.is_excluded(devices[item])
        ]
        if not others:
            raise ValueError(
                f"{saved['name']} has no other radar. Add its new radar to it first "
                "(include this radar again to see the zone), or delete the zone."
            )


def _leave_saved_groups(runtime, device_id):
    saved = runtime.data.get("room_groups", {})
    touched = set()
    for key in list(saved):
        ids = saved[key]["device_ids"]
        if device_id not in ids:
            continue
        touched.add(key)
        remaining = [item for item in ids if item != device_id]
        if remaining:
            saved[key] = {**saved[key], "device_ids": remaining}
        else:  # Only an area override can get here: restore the area's own radars.
            saved.pop(key)
    return touched


def _forget_room_results(runtime, device_id, touched):
    """Assessments of the rooms it was in, and every joint result that set its thresholds:
    they describe a membership that no longer exists and could not be applied."""
    assessments = runtime.data.get("room_assessments", {})
    for key in touched:
        assessments.pop(key, None)
    for stored in runtime.data.get("room_learning", {}).values():
        slots = stored.get("slots", {})
        for source in [
            name for name, item in slots.items() if device_id in (item or {}).get("thresholds", {})
        ]:
            slots.pop(source)


def _leave_selections(runtime, device_id):
    saved = runtime.data.get(bulk_labels.KEY) or {}
    chosen = [saved.get("last") or {}, *saved.get("templates", {}).values()]
    for item in chosen:
        if device_id in item.get("device_ids", ()):
            item["device_ids"] = [key for key in item["device_ids"] if key != device_id]


def _forget_timeout(runtime, device_id):
    task = runtime._timeout_tasks.pop(device_id, None)
    if task is not None:
        task.cancel()


def departed(runtime, identity):
    """Radars the tuner still holds data for whose entities have left Home Assistant, by
    name, for the panel to offer removing. ``identity`` gives a radar's ``(name, area_id)``."""
    from .. import entity_index

    present = entity_index.read(runtime, er.async_get(runtime.hass)).radars
    found = []
    for device_id, device in runtime.data["devices"].items():
        if device_id in present or exclusion.is_excluded(device):
            continue
        name, area_id = identity(device_id)
        found.append({"device_id": device_id, "name": name, "area_id": area_id})
    return sorted(found, key=lambda item: (item["name"].casefold(), item["device_id"]))
