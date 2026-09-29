"""Read existing device timing controls; never write them or require a package."""

import math
import re

from homeassistant.helpers import entity_registry as er

FIELDS = ("timeout", "on_delay", "off_delay")
DEFAULTS = {"mode": "device", "timeout": 1.0, "on_delay": 0.5, "off_delay": 1.0}


def settings(runtime):
    """Global policy; fallback values are inactive until explicitly selected."""
    return {**DEFAULTS, **getattr(runtime, "data", {}).get("timing_settings", {})}


def configure(runtime, values):
    if not isinstance(values, dict) or set(values) != set(DEFAULTS):
        raise ValueError("Choose a timing mode and all three fallback durations")
    if values["mode"] not in ("device", "fallback", "disabled"):
        raise ValueError("Choose device timing, fallback timing, or disabled timing")
    for field in FIELDS:
        value = values[field]
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 65535:
            raise ValueError("Timing durations must be finite seconds between 0 and 65535")
    runtime.data["timing_settings"] = dict(values)
    runtime._schedule_save()
    return settings(runtime)


def _resolve(values, policy):
    mode = policy["mode"]
    if mode == "disabled":
        return dict.fromkeys(FIELDS), dict.fromkeys(FIELDS, "disabled")
    sources = {
        field: "device" if value is not None else "unknown" for field, value in values.items()
    }
    if mode == "fallback":
        for field in FIELDS:
            if values[field] is None:
                values[field], sources[field] = policy[field], "fallback"
    return values, sources


def _names(entity):
    return [
        str(getattr(entity, key, "") or "").lower().replace(" ", "_")
        for key in ("original_name", "translation_key", "entity_id")
    ]


def _field(entity):
    names = _names(entity)
    for field, suffix in (("on_delay", "presence_on_delay"), ("off_delay", "presence_off_delay")):
        if any(name.endswith(suffix) for name in names):
            return field
    if any(re.search(r"(?:^|[._])timeout$", name) for name in names):
        if not any("training" in name for name in names):
            return "timeout"
    return None


def _seconds(state, field):
    if state is None:
        return None
    text = str(state.state).strip()
    unit = str(getattr(state, "attributes", {}).get("unit_of_measurement", ""))
    encoded = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*(ms|s|min|h)", text)
    if encoded:
        text, unit = encoded.groups()
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    scale = {"s": 1, "ms": 0.001, "min": 60, "h": 3600}.get(unit)
    if not unit and field == "timeout":
        scale = 1
    if scale is None or not math.isfinite(value) or not 0 <= value * scale <= 65535:
        return None
    return round(value * scale, 6)


def _candidates(runtime, device_id):
    candidates = {field: [] for field in FIELDS}
    for entity in er.async_get(runtime.hass).entities.values():
        if entity.device_id != device_id or entity.domain not in ("number", "sensor"):
            continue
        field = _field(entity)
        if field:
            candidates[field].append(entity.entity_id)
    return candidates


def read_timing(runtime, device_id):
    candidates = _candidates(runtime, device_id)
    values, entities = {}, {}
    for field, found in candidates.items():
        entities[field] = found[0] if len(found) == 1 else None
        values[field] = (
            _seconds(runtime.hass.states.get(found[0]), field) if len(found) == 1 else None
        )
    reported = dict(values)
    policy = settings(runtime)
    values, sources = _resolve(values, policy)
    return {
        **values,
        "entities": entities,
        "reported": reported,
        "sources": sources,
        "mode": policy["mode"],
        "scope": _scope(values),
    }


def timing_values(config):
    return {field: (config or {}).get(field) for field in FIELDS}


def _scope(values):
    if all(value is not None for value in values.values()):
        return "reported_presence"
    return "radar" if values["timeout"] is not None else "raw"


def timing_signature(config):
    """Include modeling policy and provenance when checking an in-flight fit."""
    return {
        **timing_values(config),
        "mode": (config or {}).get("mode", "device"),
        "sources": (config or {}).get("sources", {}),
    }
