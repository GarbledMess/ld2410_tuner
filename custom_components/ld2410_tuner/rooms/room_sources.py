"""Cameras, boolean entities and Bermuda attached to a room, written as sensor room labels.

Same schema, validation, buffers and confidence as device sources (presence/sources.py,
presence/source_buffer.py), keyed ``room:<group_id>``. The output is ``source: "sensor"``
room labels. They are automatic evidence for room truth (level 4) and train member radars
at the lower automatic weight; they never replace or trim a human label.

Retraining cost: ``sample_rooms`` runs every autolabelling tick. Extending an open label
changes nothing but the stored span. Opening or finalising a label (state change, source
unavailable, outage gap, reconfigure) bumps ``room_sensor_revision`` so assessments go stale.
Finalising also marks member references stale (cheap; they rebuild overnight or on demand,
presence/rebuilds.py). Like device-level sources, a sensor change never bumps member
``label_revision`` or drops learning results: the sensor revision stales assessments only.
"""

from __future__ import annotations

import time
from copy import deepcopy

from ..const import HISTORY_SAMPLE_INTERVAL
from ..presence import source_buffer, sources
from . import groups, membership, room_labels

STORE = "room_presence_sources"


def _key(group_id):
    return f"room:{group_id}"


def settings(runtime, group_id):
    saved = runtime.data.get(STORE, {}).get(group_id, {})
    return deepcopy({**sources.DEFAULTS, **saved})


def configure_room_sources(runtime, group_id, values):
    if group_id not in groups.definitions(runtime)[2]:
        raise ValueError("This group no longer exists; refresh the panel")
    validated = sources.validate(values)
    changed = validated != settings(runtime, group_id)
    _finalise(runtime, group_id)  # Close the open label under the old settings.
    runtime._room_sensor_checked.add(group_id)
    runtime._source_runtime.pop(_key(group_id), None)
    runtime.data.setdefault(STORE, {})[group_id] = validated
    if changed:
        # Part of every related assessment signature (rooms/jobs.py) but not of the manual
        # edit check, so configuring never invalidates a timeline the person is editing.
        room_labels.bump_sensor_revision(runtime, group_id)
    runtime._schedule_save()
    return settings(runtime, group_id)


def forget(runtime, group_id):
    """Drop a removed group's sources and any in-flight buffering."""
    runtime.data.get(STORE, {}).pop(group_id, None)
    runtime._source_runtime.pop(_key(group_id), None)
    runtime._room_sensor_open.pop(group_id, None)
    runtime._room_sensor_checked.discard(group_id)


def summary(runtime, group_id):
    evidence = sources.read(runtime.hass, settings(runtime, group_id))
    buffered = bool(evidence["start_buffer_seconds"] or evidence["end_buffer_seconds"])
    buffering = (
        evidence["state"] == "present"
        and buffered
        and not source_buffer.ready(runtime, _key(group_id), evidence, time.time())
    )
    return {
        **evidence,
        "raw_state": evidence["state"],
        "buffering": buffering,
        "state": "unknown" if buffering else evidence["state"],
    }


def sample_rooms(runtime, now) -> None:
    """Advance every configured room's source labels; called once per autolabelling tick."""
    if runtime._storage_status.get("blocked"):
        return
    defined = groups.definitions(runtime)[2]
    for group_id in list(runtime.data.get(STORE, {})):
        if group_id in defined:
            _tick(runtime, group_id, now)


def _tick(runtime, group_id, now):
    config = settings(runtime, group_id)
    state = sources.read(runtime.hass, config)["state"]
    if group_id not in runtime._room_sensor_checked:
        runtime._room_sensor_checked.add(group_id)
        _recover(runtime, group_id, state, now)

    def write(rt, _buffer_key, start, end, percent):
        _extend(rt, group_id, start, end, percent, now)

    source_buffer.observe(runtime, _key(group_id), state == "present", config, now, write)
    current = runtime._room_sensor_open.get(group_id)
    if current and current["state"] != state:
        _finalise(runtime, group_id)
    if state == "not_present":
        _extend_empty(runtime, group_id, now, config["confidence"])


def _recover(runtime, group_id, state, now):
    """After a restart the open label is gone: adopt a stored one still being continued,
    otherwise finalise it so its members' references are marked stale."""
    stored = [x for x in room_labels.room_labels_for(runtime, group_id) if x["source"] == "sensor"]
    if not stored:
        return
    last = max(stored, key=lambda x: x["end"])
    runtime._room_sensor_open[group_id] = {
        "state": last["state"],
        "end": last["end"],
        "written": True,
    }
    continued = last["state"] == state and now - last["end"] <= HISTORY_SAMPLE_INTERVAL * 2
    if not continued:
        _finalise(runtime, group_id)


def _extend(runtime, group_id, start, end, percent, now):
    if end <= start:
        return  # Still inside the start buffer.
    current = runtime._room_sensor_open.get(group_id)
    if current and (
        current["state"] != "present" or start - current["end"] > HISTORY_SAMPLE_INTERVAL * 2
    ):
        _finalise(runtime, group_id)  # A different state or an outage gap: a new label.
        current = None
    opening = current is None
    room_labels.write_sensor_range(
        runtime, group_id, start, end, "present", percent / 100, bump=opening
    )
    runtime._room_sensor_open[group_id] = {"state": "present", "end": end, "written": True}


def _extend_empty(runtime, group_id, now, percent):
    current = runtime._room_sensor_open.get(group_id)
    if current and now - current["end"] > HISTORY_SAMPLE_INTERVAL * 2:
        _finalise(runtime, group_id)  # Never bridge a telemetry outage.
        current = None
    if current is None:
        # The first empty reading only opens the period; its label begins at the next tick.
        runtime._room_sensor_open[group_id] = {"state": "not_present", "end": now, "written": False}
        return
    room_labels.write_sensor_range(
        runtime,
        group_id,
        current["end"],
        now,
        "not_present",
        percent / 100,
        bump=not current["written"],
    )
    runtime._room_sensor_open[group_id] = {"state": "not_present", "end": now, "written": True}


def _finalise(runtime, group_id):
    """Close the open label: bump the sensor revision and mark member references stale.
    Both are cheap, so every finalise does both."""
    closed = runtime._room_sensor_open.pop(group_id, None)
    if not (closed and closed["written"]):
        return
    room_labels.bump_sensor_revision(runtime, group_id)
    membership.labels_changed(runtime, group_id, invalidate=False)
