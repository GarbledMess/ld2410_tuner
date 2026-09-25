"""Discovery operations on the shared runtime state."""

from __future__ import annotations

from typing import Any

from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event

from ..const import GATE_RE


@callback
def subscribe_state_changes(runtime) -> None:
    """Subscribe only to the dynamically discovered LD2410 energy entities."""
    if runtime.unsub:
        runtime.unsub()
    registry = er.async_get(runtime.hass)
    entity_ids = []
    for entity in registry.entities.values():
        if entity.domain != "sensor" or not entity.device_id:
            continue
        match = GATE_RE.match(entity.entity_id.split(".", 1)[1])
        if match and match.group("metric") == "energy":
            entity_ids.append(entity.entity_id)
    runtime.unsub = async_track_state_change_event(
        runtime.hass, entity_ids, runtime.handle_state_change
    )


@callback
def handle_registry_update(runtime, event) -> None:
    runtime.refresh_devices(er.async_get(runtime.hass))
    runtime.subscribe_state_changes()


@callback
def refresh_devices(runtime, registry: er.EntityRegistry) -> None:
    before = {key: value.get("entities", {}) for key, value in runtime.data["devices"].items()}
    devices = _discover_devices(registry)

    # Keep persisted training data, but hide devices whose LD2410 entities
    # have actually been removed from the entity registry.
    for existing_id, existing in runtime.data["devices"].items():
        if existing_id not in devices:
            existing["entities"] = {}
    for device_id, info in devices.items():
        runtime.data["devices"].setdefault(device_id, {})
        runtime.data["devices"][device_id]["entities"] = info["entities"]
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

    try:
        value = float(new_state.state)
    except (TypeError, ValueError):
        return
    if not 0 <= value <= 100:
        return

    device = runtime.data["devices"].get(entity.device_id)
    if not device:
        return

    key = f"g{match.group('gate')}_{match.group('kind')}"
    runtime._live.setdefault(entity.device_id, {})[key] = value


def _discover_devices(registry):
    devices: dict[str, dict[str, Any]] = {}
    for entity in registry.entities.values():
        if entity.domain != "sensor" or not entity.device_id:
            continue
        match = GATE_RE.match(entity.entity_id.split(".", 1)[1])
        if not match or match.group("metric") != "energy":
            continue
        devices.setdefault(entity.device_id, {"entities": {}})
        devices[entity.device_id]["entities"][entity.entity_id] = {
            "gate": int(match.group("gate")),
            "kind": match.group("kind"),
        }
    return devices
