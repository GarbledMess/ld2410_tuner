"""Pure candidate coordination and bounded runtime training snapshots."""

from copy import copy


def _clip_labels(labels, cutoff):
    return [dict(item, end=min(item["end"], cutoff)) for item in labels if item["start"] < cutoff]


def _clip_device(device, cutoff):
    result = dict(device)
    result["history_labels"] = _clip_labels(device.get("history_labels", []), cutoff)
    start = device.get("training_label_start")
    if start is None or start >= cutoff:
        result["training_state"] = "unknown"
    else:
        result["training_expires_at"] = min(device.get("training_expires_at") or cutoff, cutoff)
    return result


def _clip_snapshot(saved, devices, cutoff):
    if saved is None:
        return None
    return {
        **saved,
        "members": {
            key: _clip_device({field: devices.get(key, member)[field] for field in member}, cutoff)
            for key, member in saved["members"].items()
        },
        "room_labels": {
            key: _clip_labels(labels, cutoff) for key, labels in saved["room_labels"].items()
        },
    }


def earlier_view(view, cutoff):
    """Share immutable history buffers while rebuilding all label-derived state."""
    result = copy(view)
    devices = {key: _clip_device(device, cutoff) for key, device in view.data["devices"].items()}
    result.data = {
        **view.data,
        "devices": devices,
        "room_labels": {
            key: _clip_labels(labels, cutoff)
            for key, labels in view.data.get("room_labels", {}).items()
        },
    }
    result.data["room_snapshot"] = _clip_snapshot(view.data.get("room_snapshot"), devices, cutoff)
    result._attribution_guesses = {}
    result._attribution_until = cutoff  # History is read only before the cutoff.
    return result
