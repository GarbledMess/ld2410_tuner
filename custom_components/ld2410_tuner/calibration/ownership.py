"""Protect historical learning inputs while allowing ordinary recording to continue."""


def intact(snapshot, runtime, device_ids=None):
    """Whether what a learning job read (its view of the day store, as of when it was taken)
    is still the recorded history: recording only appends, so it is unless a radar was removed
    or its recorded days were rewritten or deleted since (thinning, Trim now, Clear).

    Called at a learning job's installation boundary, never during polling.
    """
    devices = snapshot.data["devices"]
    return all(
        _device_intact(snapshot, runtime, key)
        for key in (devices if device_ids is None else device_ids)
    )


def _device_intact(snapshot, runtime, key):
    if snapshot.data["devices"].get(key) is None or runtime.data["devices"].get(key) is None:
        return False
    taken = getattr(snapshot, "_history_rewrites", {})
    days = getattr(runtime, "_days", None)
    return key not in taken or (days is not None and days.rewrites(key) == taken[key])
