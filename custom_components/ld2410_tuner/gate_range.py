"""A radar's range: the farthest gate it detects in, for movement and for stillness.

The LD2410's Max Move Distance and Max Still Distance settings (ESPHome numbers, gates 0-8)
end detection at a gate; readings farther out never make the radar present. They are not
recorded (``history.ingest`` records those gates as missing) and the charts leave them out.
A radar without the settings, or whose settings are unknown, keeps every gate.
"""

from __future__ import annotations

import math

from . import entity_index
from .const import HISTORY_KEYS

KINDS = ("move", "still")
SUFFIXES = {kind: (f"max_{kind}_distance", f"max_{kind}_distance_gate") for kind in KINDS}
DOMAINS = ("number", "sensor")
NONE_BEYOND = frozenset()


def entities(runtime, device_id):
    """``{kind: entity id or None}`` of a radar's range settings. They change only with the
    registry, so they are found once for each registry the index reads."""
    from homeassistant.helpers import entity_registry as er

    index = entity_index.read(runtime, er.async_get(runtime.hass))
    if device_id not in index.ranges:
        entries = index.by_device.get(device_id, ())
        index.ranges[device_id] = {kind: _setting(entries, kind) for kind in KINDS}
    return index.ranges[device_id]


def _setting(entries, kind):
    """The one entity among ``entries`` setting the ``kind`` range; None when not just one."""
    found = [
        entry.entity_id
        for entry in entries
        if any(
            entity_index.named(entry, domain, suffix)
            for domain in DOMAINS
            for suffix in SUFFIXES[kind]
        )
    ]
    return found[0] if len(found) == 1 else None


def read(runtime, device_id):
    """``{kind: farthest gate or None}``: None where the setting is missing or unknown."""
    found = entities(runtime, device_id)
    return {kind: _gate(runtime.hass.states.get(found[kind])) for kind in KINDS}


def _gate(state):
    if state is None:
        return None
    try:
        value = float(state.state)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return min(8, round(value))


def refresh(runtime, device_id):
    """Read the radar's range again (each poll) and keep it for the events between polls."""
    limits = read(runtime, device_id)
    _cache(runtime)[device_id] = limits
    return limits


def limits(runtime, device_id):
    """The radar's kept range, read now when nothing is kept; every gate without Home
    Assistant (a detached view)."""
    kept = _cache(runtime).get(device_id)
    if kept is not None:
        return kept
    if getattr(runtime, "hass", None) is None:
        return dict.fromkeys(KINDS)
    return refresh(runtime, device_id)


def forget(runtime):
    """The registry changed: the settings' entities may have too."""
    _cache(runtime).clear()


def beyond(runtime, device_id):
    """The gate keys (``HISTORY_KEYS``) beyond the radar's range."""
    return outside(limits(runtime, device_id))


def outside(limits):
    """The gate keys beyond ``limits`` (``{kind: farthest gate or None}``)."""
    if not any(value is not None for value in limits.values()):
        return NONE_BEYOND
    return frozenset(
        key
        for key in HISTORY_KEYS
        if (farthest := limits.get(key.split("_", 1)[1])) is not None and int(key[1]) > farthest
    )


def within(runtime, device_id, keys):
    """``keys`` without the gates beyond the radar's range, in order."""
    out = beyond(runtime, device_id)
    return [key for key in dict.fromkeys(keys) if key not in out]


def _cache(runtime):
    return runtime.__dict__.setdefault("_gate_ranges", {})
