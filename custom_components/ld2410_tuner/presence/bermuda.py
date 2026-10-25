"""Shared tracked-device assignments contribute ordinary per-radar source readings."""

from copy import deepcopy

from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

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
        _validate_device(runtime, device)
    return deepcopy(values)


def _validate_device(runtime, device):
    _object(device, {"radars"}, "Each Bermuda device requires radars")
    if not isinstance(device["radars"], dict):
        raise ValueError("Supply Bermuda radars as an object")
    for device_id, config in device["radars"].items():
        if device_id not in runtime.data["devices"]:
            raise ValueError("Unknown radar")
        _object(config, {"max_distance"}, "Each radar requires max_distance")
        if config["max_distance"] is not None:
            sources._validate_max_distance(config)


def _assignments(values, device_id):
    return {
        entity_id: device["radars"][device_id]
        for entity_id, device in values["devices"].items()
        if device_id in device["radars"]
    }


def configure(runtime, values):
    validated = validate(runtime, values)
    previous = settings(runtime)
    runtime.data["bermuda"] = validated
    for device_id in runtime.data["devices"]:
        if _assignments(previous, device_id) != _assignments(validated, device_id):
            reference_training.reset_entities(runtime.data["devices"][device_id])
            sources.reset_runtime(runtime, device_id)
    runtime._schedule_save()
    return settings(runtime)


def effective(runtime, device_id):
    area = radar_area(runtime, device_id)
    if not area:
        return []
    return [
        _source(entity_id, area, config)
        for entity_id, config in _assignments(
            runtime.data.get("bermuda", {"devices": {}}), device_id
        ).items()
    ]


def _source(entity_id, area, config):
    source = {"entity_id": entity_id, "kind": "bermuda", "area": area}
    if config["max_distance"] is not None:
        source["max_distance"] = config["max_distance"]
    return source


def migrate(runtime):
    """Only own-area legacy assignments move; intentional cross-area sources stay intact."""
    if runtime.data.get("bermuda_migrated"):
        return
    areas = {area.id: area.name for area in ar.async_get(runtime.hass).async_list_areas()}
    central = settings(runtime)
    for device_id, device in runtime.data["devices"].items():
        _migrate_radar(runtime, device_id, device, central, areas)
    runtime.data["bermuda"] = central
    runtime.data["bermuda_migrated"] = True
    runtime._schedule_save()


def _migrate_radar(runtime, device_id, device, central, areas):
    area = radar_area(runtime, device_id)
    if not area:
        return
    matches = {area.casefold(), areas.get(area, area).casefold()}
    config = sources.settings(device)
    remaining = []
    for source in config["sources"]:
        if source["kind"] == "bermuda" and source["area"].strip().casefold() in matches:
            radars = central["devices"].setdefault(source["entity_id"], {"radars": {}})["radars"]
            radars.setdefault(device_id, {"max_distance": source.get("max_distance")})
        else:
            remaining.append(source)
    if remaining != config["sources"]:
        device["presence_sources"] = {**config, "sources": remaining}
        reference_training.reset_entities(device)
        sources.reset_runtime(runtime, device_id)
