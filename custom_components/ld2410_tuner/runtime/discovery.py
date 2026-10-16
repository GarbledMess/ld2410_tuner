"""Discovery operations on the shared runtime state."""

from __future__ import annotations

import time

from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event

from .. import entity_index
from ..const import GATE_RE
from ..history import ingest
from ..rooms import groups, membership


@callback
def subscribe_state_changes(runtime) -> None:
    """Subscribe only to the dynamically discovered LD2410 energy entities."""
    if runtime.unsub:
        runtime.unsub()
    registry = er.async_get(runtime.hass)
    entity_ids = [
        entity_id
        for radar in entity_index.read(runtime, registry).radars.values()
        for entity_id in radar["entities"]
    ]
    runtime.unsub = async_track_state_change_event(
        runtime.hass, entity_ids, runtime.handle_state_change
    )


@callback
def handle_registry_update(runtime, event) -> None:
    entity_index.forget(runtime)
    before = groups.definitions(runtime)[2]
    runtime.refresh_devices(er.async_get(runtime.hass))
    # Radars that lose their entities leave their groups; keep their room evidence current.
    membership.membership_changed(runtime, before)
    runtime.subscribe_state_changes()


@callback
def refresh_devices(runtime, registry: er.EntityRegistry) -> None:
    before = {key: value.get("entities", {}) for key, value in runtime.data["devices"].items()}
    devices = entity_index.read(runtime, registry).radars

    # Keep persisted training data, but hide devices whose LD2410 entities
    # have actually been removed from the entity registry.
    for existing_id, existing in runtime.data["devices"].items():
        if existing_id not in devices:
            existing["entities"] = {}
            ingest.end(runtime, existing_id, time.time())  # Removed: nothing more is observed.
    for device_id, info in devices.items():
        runtime.data["devices"].setdefault(device_id, {"recording_enabled": False})
        runtime.data["devices"][device_id]["entities"] = dict(info["entities"])
        runtime.data["devices"][device_id].setdefault("training_state", "unknown")

    if before != {key: value.get("entities", {}) for key, value in runtime.data["devices"].items()}:
        runtime._schedule_save()


@callback
def handle_state_change(runtime, event) -> None:
    entity_id = event.data["entity_id"]
    new_state = event.data.get("new_state")
    if new_state is None:
        return

    registry = er.async_get(runtime.hass)
    entity = registry.async_get(entity_id)
    if not entity or entity.domain != "sensor" or not entity.device_id:
        return

    match = GATE_RE.match(entity_id.split(".", 1)[1])
    if not match or match.group("metric") != "energy":
        return

    device = runtime.data["devices"].get(entity.device_id)
    if not device:
        return

    key = f"g{match.group('gate')}_{match.group('kind')}"
    value = _energy(new_state.state)
    if value is not None:
        runtime._live.setdefault(entity.device_id, {})[key] = value
    ingest.change(runtime, entity.device_id, {key: value}, _event_time(new_state))


def _energy(state):
    """A delivered energy reading, or None for unavailable, non-numeric or out-of-range."""
    try:
        value = float(state)
    except (TypeError, ValueError):
        return None
    return value if 0 <= value <= 100 else None


def _event_time(new_state):
    """When Home Assistant set the state; receipt time when the state carries none."""
    updated = getattr(new_state, "last_updated", None)
    return updated.timestamp() if updated is not None else time.time()
