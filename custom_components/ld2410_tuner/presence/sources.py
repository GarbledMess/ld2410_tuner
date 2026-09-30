"""Independent HA entities supplying automatic, never human, training labels."""

import re
import time
from copy import deepcopy

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
    runtime._auto_runtime.pop(device_id, None)
    runtime._source_runtime.pop(device_id, None)
    device.get("auto", {}).pop("last_classification", None)
    runtime._schedule_save()
    return settings(device)


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
        _validate_source(source)
    return deepcopy(values)


def _validate_buffers(values):
    for key in ("start_buffer_seconds", "end_buffer_seconds"):
        value = values[key]
        if type(value) not in (int, float) or not 0 <= value <= 3600:
            raise ValueError("Presence buffers must be between 0 and 3600 seconds")


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


def _read_sources(runtime, device):
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
