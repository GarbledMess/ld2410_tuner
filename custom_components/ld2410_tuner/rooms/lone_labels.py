"""A room or zone with exactly one radar is that radar: its manual labels train it.

Each radar keeps a derived ``group_labels`` copy of the manual labels of every group whose
only member it is. ``label_lookup.label_index`` reads them before the radar's own labels,
so the radar's own labels win wherever both exist. Recomputed whenever labels or
membership change (``membership._refresh``) and when devices are rediscovered.
"""

from __future__ import annotations

from ..history import labels
from . import groups


def _derived(runtime):
    labels = runtime.data.get("room_labels", {})
    devices = runtime.data["devices"]
    found = {}
    for key, item in groups.definitions(runtime)[2].items():
        # Like areas, a saved zone counts only its recording radars.
        recording = [
            member
            for member in item["device_ids"]
            if devices.get(member, {}).get("recording_enabled", True)
        ]
        if len(recording) != 1:
            continue
        found.setdefault(recording[0], []).extend(
            {"start": x["start"], "end": x["end"], "state": x["state"]}
            for x in labels.get(key, [])
            if x.get("source") == "manual"
        )
    return {key: sorted(items, key=lambda x: x["start"]) for key, items in found.items() if items}


def sync(runtime) -> set[str]:
    """Refresh every radar's ``group_labels`` and, where they changed, its labelled
    histograms, as a label edit of its own would; the ids whose value changed."""
    derived = _derived(runtime)
    changed = set()
    for key, device in runtime.data["devices"].items():
        value = derived.get(key)
        if device.get("group_labels") == value:
            continue
        if value is None:
            device.pop("group_labels", None)
        else:
            device["group_labels"] = value
        changed.add(key)
    for key in changed:
        labels._native_label_rebuild(runtime, key, runtime.data["devices"][key])
    return changed


def refresh(runtime) -> set[str]:
    """``sync`` at startup and rediscovery: label caches and references follow the new
    labels, but saved learning results are kept (an upgrade alone must not discard them)."""
    from ..presence import rebuilds

    changed = sync(runtime)
    devices = runtime.data["devices"]
    for key in changed:
        devices[key]["label_revision"] = devices[key].get("label_revision", 0) + 1
    rebuilds.mark_stale(runtime, sorted(changed))
    if changed:
        runtime._schedule_save()
    return changed
