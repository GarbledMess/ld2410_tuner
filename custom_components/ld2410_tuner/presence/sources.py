"""Independent HA entities supplying automatic, never human, training labels."""

import re
from copy import deepcopy

from ..calibration.constants import MIN_AUTO_CONFIDENCE

DEFAULTS = {"sources": [], "mark_not_present": False, "confidence": 90}
UNKNOWN = {"unknown", "unavailable", "", "none"}


def settings(device):
    return deepcopy({**DEFAULTS, **device.get("presence_sources", {})})


def configure(runtime, device_id, values):
    device = runtime.data["devices"].get(device_id)
    if device is None:
        raise ValueError("Unknown device")
    validated = validate(values)
    device["presence_sources"] = validated
    runtime._auto_runtime.pop(device_id, None)
    device.get("auto", {}).pop("last_classification", None)
    runtime._schedule_save()
    return settings(device)


def validate(values):
    if not isinstance(values, dict) or set(values) != set(DEFAULTS):
        raise ValueError("Supply sources, mark_not_present and confidence")
    if type(values["mark_not_present"]) is not bool:
        raise ValueError("Mark Not Present must be enabled or disabled")
    confidence = values["confidence"]
    if type(confidence) not in (int, float) or not 1 <= confidence <= 100:
        raise ValueError("Source confidence must be between 1 and 100 percent")
    sources = values["sources"]
    if not isinstance(sources, list) or len(sources) > 16:
        raise ValueError("Choose at most 16 presence sources")
    for source in sources:
        _validate_source(source)
    return deepcopy(values)


def _validate_source(source):
    if not isinstance(source, dict) or source.get("kind") not in ("boolean", "bermuda"):
        raise ValueError("Choose Boolean entity or Bermuda area")
    if set(source) != {"entity_id", "kind", "area"}:
        raise ValueError("Each source requires an entity, kind and area")
    entity_id = source["entity_id"]
    if not isinstance(entity_id, str) or not re.fullmatch(r"[a-z_]+\.[a-z0-9_]+", entity_id):
        raise ValueError("Enter a valid Home Assistant entity ID")
    if not isinstance(source["area"], str) or len(source["area"]) > 255:
        raise ValueError("Enter an area ID or name")
    if source["kind"] == "bermuda" and not source["area"].strip():
        raise ValueError("Bermuda sources need the target area ID or name")


def source_state(hass, source):
    entity = hass.states.get(source["entity_id"])
    if entity is None or str(entity.state).lower() in UNKNOWN:
        return None
    if source["kind"] == "boolean":
        return {"on": True, "off": False, "true": True, "false": False}.get(
            str(entity.state).lower()
        )
    attributes = entity.attributes
    target = source["area"].strip().casefold()
    # Bermuda's current Area sensor exposes area_id/name. Do not use last-seen
    # sensors, the scanner's assigned area, or a home/away device tracker.
    current = [attributes.get("area_id"), attributes.get("area_name"), entity.state]
    return target in {str(value).casefold() for value in current if value is not None}


def summary(runtime, device):
    config = settings(device)
    readings = [
        {**source, "present": source_state(runtime.hass, source)} for source in config["sources"]
    ]
    states = [reading["present"] for reading in readings]
    label = "unknown"
    if True in states:
        label = "present"
    elif states and all(state is False for state in states) and config["mark_not_present"]:
        label = "not_present"
    return {
        **config,
        "readings": readings,
        "state": label,
        "learning_minimum_confidence": MIN_AUTO_CONFIDENCE * 100,
    }


def estimate(runtime, device):
    evidence = summary(runtime, device)
    if evidence["state"] == "unknown":
        return None
    confidence = evidence["confidence"] / 100
    present = evidence["state"] == "present"
    return {
        "state": evidence["state"],
        "confidence": confidence,
        "score": confidence if present else 1 - confidence,
        "presence_probability": confidence if present else 1 - confidence,
        "active_gates": 0,
        "top_gates": [],
        "basis": "external",
        "model": "entity-sources-v1",
        "sources": [source["entity_id"] for source in evidence["readings"]],
    }
