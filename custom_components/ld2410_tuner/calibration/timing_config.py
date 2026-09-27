"""Read existing device timing controls; never write them or require a package."""

import math
import re

from homeassistant.helpers import entity_registry as er

FIELDS = ("timeout", "on_delay", "off_delay")


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
    return {**values, "entities": entities, "scope": _scope(values)}


def timing_values(config):
    return {field: (config or {}).get(field) for field in FIELDS}


def _scope(values):
    if all(value is not None for value in values.values()):
        return "reported_presence"
    return "radar" if values["timeout"] is not None else "raw"
