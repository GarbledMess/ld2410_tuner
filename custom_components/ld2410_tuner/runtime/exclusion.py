"""Excluding a radar: the tuner leaves it out of everything until it is included again.

An excluded radar is treated as if its entities had left Home Assistant (``discovery``): it is
not recorded, learned, applied, grouped into rooms or shown, while everything stored for it
(history, labels, results, thresholds) is kept. Including it again restores its recording
setting from before it was excluded. Home Assistant's radar and entities are not touched.
"""

import time

from homeassistant.helpers import entity_registry as er

from ..history import storage
from ..rooms import groups, membership

KEY = "excluded"
REFUSED = "This radar is excluded; include it again first"


def is_excluded(device):
    return bool((device or {}).get(KEY))


def require_included(runtime, device_id):
    if is_excluded(runtime.data["devices"].get(device_id)):
        raise ValueError(REFUSED)


def set_excluded(runtime, device_id, excluded):
    if type(excluded) is not bool:
        raise ValueError("A radar must be excluded or included")
    device = runtime.data["devices"].get(device_id)
    if device is None:
        raise ValueError("Unknown device")
    if is_excluded(device) == excluded:
        return {"excluded": excluded}
    if excluded:
        recording = device.get("recording_enabled", True)
        if recording:
            storage.set_recording(runtime, device_id, False)
        _regroup(
            runtime, lambda: device.update({KEY: {"since": time.time(), "recording": recording}})
        )
    else:
        saved = device[KEY]
        _regroup(runtime, lambda: device.pop(KEY))
        if saved.get("recording", True):
            storage.set_recording(runtime, device_id, True)
    runtime._schedule_save()
    return {"excluded": excluded}


def _regroup(runtime, change):
    before = groups.definitions(runtime)[2]
    change()
    runtime.refresh_devices(er.async_get(runtime.hass))
    membership.membership_changed(runtime, before)


def listed(runtime, identity):
    """The excluded radars, by name, for the panel's tucked-away section. ``identity`` gives
    a radar's ``(name, area_id)``."""
    found = []
    for device_id, device in runtime.data["devices"].items():
        if not is_excluded(device):
            continue
        name, area_id = identity(device_id)
        found.append(
            {
                "device_id": device_id,
                "name": name,
                "area_id": area_id,
                "since": device[KEY].get("since"),
            }
        )
    return sorted(found, key=lambda item: (item["name"].casefold(), item["device_id"]))
