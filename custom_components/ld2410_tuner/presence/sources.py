"""Independent HA entities supplying automatic, never human, training labels."""

import math
import re
import time
from copy import deepcopy

from homeassistant.helpers import entity_registry as er

from ..calibration.constants import MIN_AUTO_CONFIDENCE
from . import reference_training, source_buffer

DEFAULTS = {
    "sources": [],
    "mark_not_present": False,
    "confidence": 90,
    "start_buffer_seconds": 10,
    "end_buffer_seconds": 10,
}
UNKNOWN = {"unknown", "unavailable", "", "none"}


def settings(device):
    return deepcopy({**DEFAULTS, **device.get("presence_sources", {})})


def configure(runtime, device_id, values):
    device = runtime.data["devices"].get(device_id)
    if device is None:
        raise ValueError("Unknown device")
    validated = validate(values)
    if validated != settings(device):
        reference_training.reset_entities(device)
    device["presence_sources"] = validated
    reset_runtime(runtime, device_id)
    runtime._schedule_save()
    return settings(device)


def reset_runtime(runtime, device_id):
    device = runtime.data["devices"][device_id]
    runtime._auto_runtime.pop(device_id, None)
    runtime._source_runtime.pop(device_id, None)
    device.get("auto", {}).pop("last_classification", None)


def validate(values):
    required = {"sources", "mark_not_present", "confidence"}
    if not isinstance(values, dict) or not required <= set(values) or set(values) - set(DEFAULTS):
        raise ValueError(
            "Supply sources, mark_not_present, confidence and optional presence buffers"
        )
    values = {**DEFAULTS, **values}
    _validate_buffers(values)
    if type(values["mark_not_present"]) is not bool:
        raise ValueError("Mark Not Present must be enabled or disabled")
    confidence = values["confidence"]
    if type(confidence) not in (int, float) or not 1 <= confidence <= 100:
        raise ValueError("Source confidence must be between 1 and 100 percent")
    sources = values["sources"]
    if not isinstance(sources, list) or len(sources) > 16:
        raise ValueError("Choose at most 16 presence sources")
    for source in sources:
        _validate_editor_source(source)
    return deepcopy(values)


def _validate_editor_source(source):
    if isinstance(source, dict) and source.get("kind") == "bermuda":
        raise ValueError("Configure tracked devices in the Bermuda section")
    _validate_source(source)


def _validate_buffers(values):
    for key in ("start_buffer_seconds", "end_buffer_seconds"):
        value = values[key]
        if type(value) not in (int, float) or not 0 <= value <= 3600:
            raise ValueError("Presence buffers must be between 0 and 3600 seconds")


def _validate_source(source):
    if not isinstance(source, dict) or source.get("kind") not in ("boolean", "bermuda"):
        raise ValueError("Choose Boolean entity or Bermuda area")
    keys = set(source) - ({"max_distance"} if source["kind"] == "bermuda" else set())
    if keys != {"entity_id", "kind", "area"}:
        raise ValueError("Each source requires an entity, kind and area")
    _validate_max_distance(source)
    entity_id = source["entity_id"]
    if not isinstance(entity_id, str) or not re.fullmatch(r"[a-z_]+\.[a-z0-9_]+", entity_id):
        raise ValueError("Enter a valid Home Assistant entity ID")
    if not isinstance(source["area"], str) or len(source["area"]) > 255:
        raise ValueError("Enter an area ID or name")
    if source["kind"] == "bermuda" and not source["area"].strip():
        raise ValueError("Bermuda sources need the target area ID or name")


def _validate_max_distance(source):
    if "max_distance" not in source:
        return
    value = source["max_distance"]
    if type(value) not in (int, float) or not 0 < value <= 100:
        raise ValueError("Maximum distance must be greater than 0 and at most 100 metres")


def _distance(hass, source):
    registry = er.async_get(hass)
    entry = registry.async_get(source["entity_id"])
    if entry is None or entry.platform != "bermuda":
        return None
    entity_id = registry.async_get_entity_id("sensor", "bermuda", f"{entry.unique_id}_range")
    entity = hass.states.get(entity_id) if entity_id else None
    try:
        value = float(entity.state) if entity else None
    except (ValueError, TypeError):
        return None
    return value if value is not None and math.isfinite(value) else None


def source_state(hass, source):
    return _reading(hass, source)["present"]


def _entity_state(hass, source):
    """The source entity's own answer: on/off, or whether Bermuda puts it in the area."""
    entity = hass.states.get(source["entity_id"])
    if entity is None or str(entity.state).lower() in UNKNOWN:
        return None
    if source["kind"] == "boolean":
        return {"on": True, "off": False, "true": True, "false": False}.get(
            str(entity.state).lower()
        )
    attributes = entity.attributes
    areas = source["area"] if isinstance(source["area"], list) else [source["area"]]
    targets = {area.strip().casefold() for area in areas}
    # Bermuda's current Area sensor exposes area_id/name. Do not use last-seen
    # sensors, the scanner's assigned area, or a home/away device tracker.
    current = [attributes.get("area_id"), attributes.get("area_name"), entity.state]
    return bool(targets & {str(value).casefold() for value in current if value is not None})


def _reading(hass, source):
    """Bermuda supplies only presence; retain distance diagnostics after an area match."""
    present = _entity_state(hass, source)
    if source["kind"] == "bermuda":
        present = True if present else None
    if not present or "max_distance" not in source:
        return {**source, "present": present}
    distance = _distance(hass, source)
    present = True if distance is not None and distance <= source["max_distance"] else None
    return {**source, "present": present, "distance": distance}


def _read_sources(runtime, device):
    return read(runtime.hass, settings(device))


def read(hass, config):
    """Read every configured source and reduce them to one present/not_present/unknown state."""
    readings = [_reading(hass, source) for source in config["sources"]]
    states = [reading["present"] for reading in readings]
    booleans = [reading["present"] for reading in readings if reading["kind"] == "boolean"]
    label = "unknown"
    if True in states:
        label = "present"
    elif booleans and all(state is False for state in booleans) and config["mark_not_present"]:
        label = "not_present"
    return {
        **config,
        "readings": readings,
        "state": label,
        "learning_minimum_confidence": MIN_AUTO_CONFIDENCE * 100,
    }


def _device_id(runtime, device):
    return next(key for key, value in runtime.data["devices"].items() if value is device)


def _buffered(config):
    return bool(config["start_buffer_seconds"] or config["end_buffer_seconds"])


def summary(runtime, device):
    evidence = _read_sources(runtime, device)
    buffering = (
        evidence["state"] == "present"
        and _buffered(evidence)
        and not source_buffer.ready(runtime, _device_id(runtime, device), evidence, time.time())
    )
    return {
        **evidence,
        "sources": settings(device)["sources"],
        "raw_state": evidence["state"],
        "buffering": buffering,
        "state": "unknown" if buffering else evidence["state"],
    }


def estimate(runtime, device, device_id=None, now=None):
    evidence = _read_sources(runtime, device)
    now = time.time() if now is None else now
    device_id = _device_id(runtime, device) if device_id is None else device_id
    positive = evidence["state"] == "present"
    buffered = _buffered(evidence)
    confirmed = (
        source_buffer.observe(runtime, device_id, positive, evidence, now) if buffered else True
    )
    if evidence["state"] == "unknown":
        return None
    confidence = evidence["confidence"] / 100
    return {
        "state": "unknown" if positive and not confirmed else evidence["state"],
        "confidence": confidence,
        "score": confidence if positive else 1 - confidence,
        "presence_probability": confidence if positive else 1 - confidence,
        "active_gates": 0,
        "top_gates": [],
        "basis": "external",
        "model": "entity-sources-v2",
        "buffered": positive and buffered,
        "sources": [source["entity_id"] for source in evidence["readings"]],
    }
