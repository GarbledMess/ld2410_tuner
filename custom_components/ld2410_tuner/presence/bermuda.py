"""Shared tracked-device assignments contribute ordinary per-radar source readings."""

from copy import deepcopy

from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from ..rooms import groups, room_sources
from . import reference_training, sources

NO_AREA = "Assign this radar a Home Assistant area to use Bermuda."


def settings(runtime):
    return deepcopy(runtime.data.get("bermuda", {"devices": {}}))


def devices(runtime):
    """A range sibling identifies Area sensors even when their entity IDs were renamed."""
    registry = er.async_get(runtime.hass)
    found = []
    for entity_id, entry in registry.entities.items():
        if not entity_id.startswith("sensor.") or getattr(entry, "platform", None) != "bermuda":
            continue
        sibling = registry.async_get_entity_id("sensor", "bermuda", f"{entry.unique_id}_range")
        if sibling:
            found.append(_device(runtime.hass, entity_id, entry))
    return sorted(found, key=lambda item: (item["name"].casefold(), item["entity_id"]))


def _device(hass, entity_id, entry):
    state = hass.states.get(entity_id)
    attributes = state.attributes if state else {}
    name = (
        attributes.get("friendly_name")
        or getattr(entry, "name", None)
        or getattr(entry, "original_name", None)
        or entity_id
    )
    area = attributes.get("area_name") or (state.state if state else None)
    return {
        "entity_id": entity_id,
        "name": name,
        "area": area,
        "area_id": attributes.get("area_id"),
    }


def radar_area(runtime, device_id):
    entry = dr.async_get(runtime.hass).async_get(device_id)
    return entry.area_id if entry else None


def radars(runtime):
    result = {}
    for device_id in runtime.data["devices"]:
        area = radar_area(runtime, device_id)
        result[device_id] = {"area_id": area, "reason": None if area else NO_AREA}
    return result


def _object(value, keys, message):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError(message)


def validate(runtime, values):
    _object(values, {"devices"}, "Supply Bermuda devices")
    entries = values["devices"]
    if not isinstance(entries, dict):
        raise ValueError("Supply Bermuda devices as an object")
    for entity_id, device in entries.items():
        # Use the same entity-ID rules as legacy sources; unavailable devices remain editable.
        sources._validate_source({"entity_id": entity_id, "kind": "bermuda", "area": "area"})
        _validate_device(runtime, entity_id, device)
    return deepcopy(values)


def _validate_device(runtime, entity_id, device):
    if not isinstance(device, dict) or "radars" not in device or set(device) - {"radars", "rooms"}:
        raise ValueError("Each Bermuda device requires radars and optional rooms")
    known = {"radars": runtime.data["devices"], "rooms": groups.definitions(runtime)[2]}
    for kind in ("radars", "rooms"):
        entries = device.get(kind, {})
        if not isinstance(entries, dict):
            raise ValueError(f"Supply Bermuda {kind} as an object")
        for target, config in entries.items():
            if target not in known[kind]:
                raise ValueError(f"Unknown {kind} target")
            _validate_target(runtime, entity_id, kind, target, config)


def _validate_target(runtime, entity_id, kind, target, config):
    if (
        not isinstance(config, dict)
        or "max_distance" not in config
        or set(config) - {"max_distance", "area"}
    ):
        raise ValueError("Each target requires max_distance and optional area")
    if config["max_distance"] is not None:
        sources._validate_max_distance(config)
    area = config.get("area")
    if area is None:
        return
    known = groups.definitions(runtime)[0]
    previous = _assignments(settings(runtime), target, kind).get(entity_id, {})
    # Only an unchanged migrated value may retain an area no longer in the registry.
    if not isinstance(area, str) or (area not in known and area != previous.get("area")):
        raise ValueError("Choose an existing Home Assistant area, or Own area")


def _assignments(values, target, kind="radars"):
    return {
        entity_id: device.get(kind, {})[target]
        for entity_id, device in values["devices"].items()
        if target in device.get(kind, {})
    }


def default_area(runtime, target, kind="radars"):
    if kind == "radars":
        return radar_area(runtime, target)
    group = groups.definitions(runtime)[2].get(target, {})
    if group.get("area_id"):
        return group["area_id"]
    return sorted(
        {
            area
            for device_id in group.get("device_ids", [])
            if (area := radar_area(runtime, device_id))
        }
    )


def configure(runtime, values):
    validated = validate(runtime, values)
    previous = settings(runtime)
    runtime.data["bermuda"] = validated
    for device_id in runtime.data["devices"]:
        if _assignments(previous, device_id) != _assignments(validated, device_id):
            reference_training.reset_entities(runtime.data["devices"][device_id])
            sources.reset_runtime(runtime, device_id)
    for group_id in groups.definitions(runtime)[2]:
        if _assignments(previous, group_id, "rooms") != _assignments(validated, group_id, "rooms"):
            room_sources.assignments_changed(runtime, group_id)
    runtime._schedule_save()
    return settings(runtime)


def effective(runtime, target, kind="radars"):
    default = default_area(runtime, target, kind)
    return [
        _source(entity_id, area, config)
        for entity_id, config in _assignments(settings(runtime), target, kind).items()
        if (area := config.get("area") or default)
    ]


def _source(entity_id, area, config):
    source = {"entity_id": entity_id, "kind": "bermuda", "area": area}
    if config["max_distance"] is not None:
        source["max_distance"] = config["max_distance"]
    return source


def migrate(runtime):
    """Upgrade even stores that completed the earlier own-area-only migration."""
    if runtime.data.get("bermuda_targets_migrated"):
        return
    areas = {area.id: area.name for area in ar.async_get(runtime.hass).async_list_areas()}
    central = settings(runtime)
    for device_id, device in runtime.data["devices"].items():
        config = sources.settings(device)
        if _move_sources(runtime, central, areas, "radars", device_id, config):
            device["presence_sources"] = config
            reference_training.reset_entities(device)
            sources.reset_runtime(runtime, device_id)
    for group_id in runtime.data.get(room_sources.STORE, {}):
        config = room_sources.settings(runtime, group_id)
        if _move_sources(runtime, central, areas, "rooms", group_id, config):
            runtime.data[room_sources.STORE][group_id] = config
            room_sources.assignments_changed(runtime, group_id, recover=True)
    runtime.data["bermuda"] = central
    runtime.data["bermuda_migrated"] = True
    runtime.data["bermuda_targets_migrated"] = True
    runtime._schedule_save()


def _area_id(value, areas):
    match = value.strip().casefold()
    return next(
        (key for key, name in areas.items() if match in {key.casefold(), name.casefold()}), value
    )


def _move_sources(runtime, central, areas, kind, target, config):
    default = default_area(runtime, target, kind)
    remaining = []
    for source in config["sources"]:
        if source["kind"] != "bermuda":
            remaining.append(source)
            continue
        area = _area_id(source["area"], areas)
        _assign_legacy(
            runtime,
            central,
            kind,
            target,
            source,
            {
                "max_distance": source.get("max_distance"),
                "area": None if default in (area, [area]) else area,
            },
        )
    changed = remaining != config["sources"]
    config["sources"] = remaining
    return changed


def _assign_legacy(runtime, central, kind, target, source, config):
    device = central["devices"].setdefault(source["entity_id"], {"radars": {}})
    chosen = device.setdefault(kind, {}).setdefault(target, config)
    # One target has one choice. Preserve displaced legacy values for recovery.
    if any(chosen.get(key) != config[key] for key in ("area", "max_distance")):
        runtime.data.setdefault("bermuda_migration_conflicts", []).append(
            {
                "target_kind": kind,
                "target_id": target,
                "source": deepcopy(source),
            }
        )
