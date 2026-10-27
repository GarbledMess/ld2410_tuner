"""Shared tracked devices contribute positive-only area-room source readings."""

from copy import deepcopy

from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from ..rooms import groups, room_sources
from . import reference_training, sources

DEFAULTS = {"devices": [], "max_distance": None, "rooms": {}}


def settings(runtime):
    return deepcopy(runtime.data.get("bermuda", DEFAULTS))


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


def _object(value, keys, message):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError(message)


def _range(value):
    if value is not None:
        sources._validate_max_distance({"max_distance": value})


def _area_rooms(runtime):
    areas, _, defined = groups.definitions(runtime)
    return {
        key: group
        for key, group in defined.items()
        if group["kind"] == "area"
        and group.get("area_id") in areas
        and key == f"area:{group.get('area_id')}"
        and groups.recording_members(group, runtime.data["devices"])
    }


def validate(runtime, values):
    _object(values, DEFAULTS, "Supply Bermuda devices, max_distance and rooms")
    entries = values["devices"]
    if not isinstance(entries, list):
        raise ValueError("Supply Bermuda devices as a list")
    known = {item["entity_id"] for item in devices(runtime)} | set(settings(runtime)["devices"])
    seen = set()
    for entity_id in entries:
        sources._validate_source({"entity_id": entity_id, "kind": "bermuda", "area": "area"})
        if entity_id in seen or entity_id not in known:
            raise ValueError("Choose distinct discovered or saved Bermuda devices")
        seen.add(entity_id)
    _range(values["max_distance"])
    rooms = values["rooms"]
    if not isinstance(rooms, dict):
        raise ValueError("Supply Bermuda rooms as an object")
    current = _area_rooms(runtime)
    for key, config in rooms.items():
        if key not in current:
            raise ValueError("Choose a current area room with recording radars")
        _object(config, {"max_distance"}, "Each room requires max_distance")
        _range(config["max_distance"])
    return deepcopy(values)


def configure(runtime, values):
    validated = validate(runtime, values)
    previous = settings(runtime)
    targets = set(previous["rooms"]) | set(validated["rooms"])
    before = {key: _effective(runtime, key, previous) for key in targets}
    runtime.data["bermuda"] = validated
    for key in sorted(targets):
        if before[key] != effective(runtime, key):
            room_sources.assignments_changed(runtime, key)
    runtime._schedule_save()
    return settings(runtime)


def effective(runtime, group_id):
    return _effective(runtime, group_id, settings(runtime))


def _effective(runtime, group_id, values):
    group = _area_rooms(runtime).get(group_id)
    if not group or group_id not in values["rooms"]:
        return []
    distance = values["rooms"][group_id]["max_distance"]
    if distance is None:
        distance = values["max_distance"]
    return [
        _source(entity_id, group["area_id"], {"max_distance": distance})
        for entity_id in sorted(values["devices"])
    ]


def _source(entity_id, area, config):
    source = {"entity_id": entity_id, "kind": "bermuda", "area": area}
    if config["max_distance"] is not None:
        source["max_distance"] = config["max_distance"]
    return source


def migrate(runtime):
    """Run the local-source upgrade first, then convert central targets once."""
    if runtime.data.get("bermuda_rooms_migrated"):
        return
    saved = deepcopy(runtime.data.get("bermuda", {"devices": {}}))
    already_new = isinstance(saved["devices"], list)
    central = {"devices": {}} if already_new else saved
    if not runtime.data.get("bermuda_targets_migrated"):
        _migrate_targets(runtime, central)
    converted = _migrate_rooms(runtime, central)
    if already_new:
        # Do not revalidate saved devices against temporarily unavailable discovery.
        converted["devices"] = saved["devices"] + [
            key for key in converted["devices"] if key not in saved["devices"]
        ]
        converted["rooms"].update(saved["rooms"])
        converted["max_distance"] = saved["max_distance"]
    runtime.data["bermuda"] = converted
    for group_id in converted["rooms"]:
        room_sources.assignments_changed(runtime, group_id, recover=True)
    runtime.data["bermuda_migrated"] = True
    runtime.data["bermuda_targets_migrated"] = True
    runtime._schedule_save()
    runtime.data["bermuda_rooms_migrated"] = True


def _legacy_default_area(runtime, target, kind):
    if kind == "radars":
        entry = dr.async_get(runtime.hass).async_get(target)
        return entry.area_id if entry else None
    return groups.definitions(runtime)[2].get(target, {}).get("area_id")


def _migrate_targets(runtime, central):
    areas = {area.id: area.name for area in ar.async_get(runtime.hass).async_list_areas()}
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


def _legacy_targets(central):
    for entity_id, device in sorted(central["devices"].items()):
        for kind in ("radars", "rooms"):
            for target, config in device.get(kind, {}).items():
                yield entity_id, kind, target, config


def _migrate_rooms(runtime, central):
    result = deepcopy(DEFAULTS)
    areas = {area.id: area.name for area in ar.async_get(runtime.hass).async_list_areas()}
    current = _area_rooms(runtime)
    enabled = set()
    for entity_id, kind, target, config in _legacy_targets(central):
        enabled.add(entity_id)
        area = config.get("area") or _legacy_default_area(runtime, target, kind)
        area_id = _area_id(area, areas) if area else None
        group_id = f"area:{area_id}"
        if group_id not in current:
            _conflict(runtime, kind, target, _source(entity_id, area, config))
            continue
        _merge_range(result["rooms"], group_id, config.get("max_distance"))
    result["devices"] = sorted(enabled)
    return result


def _merge_range(rooms, group_id, distance):
    if group_id not in rooms:
        rooms[group_id] = {"max_distance": distance}
        return
    previous = rooms[group_id]["max_distance"]
    rooms[group_id]["max_distance"] = (
        None if previous is None or distance is None else max(previous, distance)
    )


def _conflict(runtime, kind, target, source):
    runtime.data.setdefault("bermuda_migration_conflicts", []).append(
        {"target_kind": kind, "target_id": target, "source": deepcopy(source)}
    )


def _area_id(value, areas):
    match = value.strip().casefold()
    return next(
        (key for key, name in areas.items() if match in {key.casefold(), name.casefold()}), value
    )


def _move_sources(runtime, central, areas, kind, target, config):
    default = _legacy_default_area(runtime, target, kind)
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
        _conflict(runtime, kind, target, source)
