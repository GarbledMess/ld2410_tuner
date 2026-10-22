"""Keep per-radar room evidence current when room labels or group membership change."""

from __future__ import annotations

import time

from . import attribution, groups, lone_labels, truth


def _shape(definitions):
    return {
        key: (item.get("kind"), item.get("area_id"), tuple(item["device_ids"]))
        for key, item in definitions.items()
    }


def _members(definitions, group_ids):
    return {
        key
        for related in truth.related_to(definitions, group_ids)
        for key in definitions.get(related, {}).get("device_ids", ())
    }


def retrain(runtime, device_ids, invalidate=True) -> None:
    """Do for each radar what a device-label edit does: mark its references stale and,
    with ``invalidate``, its learning too (spec 5: stale when room or radar labels change).
    Both are immediate and cheap; references rebuild overnight or on demand
    (``presence/rebuilds.py``)."""
    from ..presence import rebuilds

    devices = runtime.data["devices"]
    changed = [key for key in sorted(device_ids) if key in devices]
    if invalidate:
        for key in changed:
            devices[key].pop("last_learning", None)
            devices[key]["label_revision"] = devices[key].get("label_revision", 0) + 1
    rebuilds.mark_stale(runtime, changed)
    if changed:
        runtime._schedule_save()


def _refresh(runtime, group_ids, before, after, invalidate=True) -> None:
    runtime._room_membership = attribution.membership(after, time.monotonic())
    lone = lone_labels.sync(runtime)
    affected = _members(before, group_ids) | _members(after, group_ids)
    grouped = attribution.grouped(before) | attribution.grouped(after)
    retrain(runtime, (affected & grouped) | lone, invalidate)


def labels_changed(runtime, group_id, invalidate=True) -> None:
    """After a room label edit: retrain grouped radars whose room truth it can change.
    ``invalidate`` False (room sensor labels) marks references stale only, as device sources
    do: member label revisions and learning results stay valid."""
    definitions = groups.definitions(runtime)[2]
    _refresh(runtime, {group_id}, definitions, definitions, invalidate)


def membership_changed(runtime, before) -> None:
    """After a membership change: retrain the members, old and new, of every changed group.
    ``before`` is the group definitions read before the change."""
    after = groups.definitions(runtime)[2]
    old, new = _shape(before), _shape(after)
    changed = {key for key in old.keys() | new.keys() if old.get(key) != new.get(key)}
    _refresh(runtime, changed, before, after)
