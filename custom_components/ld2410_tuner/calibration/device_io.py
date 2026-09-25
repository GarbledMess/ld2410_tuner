"""Paced radar writes and bounded recovery of missing configuration states.

ESPHome publishes threshold changes optimistically. Matching HA states are useful
checks, but are not a hardware acknowledgement, even after Query Params.
"""

from __future__ import annotations

import asyncio
import math
from contextlib import contextmanager

from homeassistant.helpers import entity_registry as er

SETTLE_SECONDS = 1.0
SERVICE_TIMEOUT = 5.0
RECOVERY_ATTEMPTS = 2
READBACK_NOTE = (
    "Reported thresholds match after paced writes. ESPHome reports requested values "
    "optimistically, so this is not proof of radar persistence. Use Query Params "
    "and check the values again; see the ESPHome recovery guide."
)


@contextmanager
def device_operation(runtime, device_id):
    """Keep manual writes and Apply from interleaving on the same radar."""
    if device_id in runtime._applying:
        raise ValueError("Threshold application is already in progress")
    runtime._applying.add(device_id)
    try:
        yield
    finally:
        runtime._applying.discard(device_id)


def number_value(state):
    """Read a numeric HA state without inventing values for unknown entities."""
    try:
        return float(state.state) if state else float("nan")
    except (TypeError, ValueError):
        return float("nan")


def distance_kind(entity_id):
    """Support both documented ESPHome names for the gate-limit numbers."""
    for kind in ("move", "still"):
        if entity_id.endswith((f"max_{kind}_distance_gate", f"max_{kind}_distance")):
            return kind
    return None


def query_button(runtime, device_id):
    """Select only an unambiguous, enabled, same-device Query Params button."""
    matches = [
        entry.entity_id
        for entry in er.async_get(runtime.hass).entities.values()
        if entry.device_id == device_id
        and entry.domain == "button"
        and not getattr(entry, "disabled_by", None)
        and entry.entity_id.endswith(("_query_params", "_query_parameters"))
    ]
    if len(matches) > 1:
        raise ValueError("Multiple Query Params buttons found; use one radar per HA device")
    return matches[0] if matches else None


async def _call(runtime, domain, service, data):
    try:
        async with asyncio.timeout(SERVICE_TIMEOUT):
            await runtime.hass.services.async_call(domain, service, data, blocking=True)
    except Exception as error:
        raise ValueError(
            f"{domain}.{service} failed; check device connectivity: {error}"
        ) from error


async def refresh_parameters(runtime, button):
    """Request settings without restarting the radar or altering Bluetooth."""
    if button:
        await _call(runtime, "button", "press", {"entity_id": button})
        await asyncio.sleep(SETTLE_SECONDS)


def _required_numbers(runtime, device_id, keys):
    required = {}
    for key in keys:
        gate = key.split("_")[0]
        for kind in ("move", "still"):
            entity_id = runtime._find_threshold_entity(device_id, f"{gate}_{kind}")
            required[entity_id] = (0, 100)
    for entry in er.async_get(runtime.hass).entities.values():
        if (
            entry.device_id == device_id
            and entry.domain == "number"
            and distance_kind(entry.entity_id)
        ):
            required[entry.entity_id] = (2, 8)
    return required


def _missing_numbers(runtime, required):
    missing = []
    for entity_id, (low, high) in required.items():
        value = number_value(runtime.hass.states.get(entity_id))
        if not math.isfinite(value) or not low <= value <= high or value != int(value):
            missing.append(entity_id)
    return missing


async def prepare_device(runtime, device_id, keys):
    """Refresh when available; refuse writes until gate pairs and limits load."""
    required = _required_numbers(runtime, device_id, keys)
    button = query_button(runtime, device_id)
    if not _missing_numbers(runtime, required):
        return button
    for _ in range(RECOVERY_ATTEMPTS):
        await refresh_parameters(runtime, button)
        missing = _missing_numbers(runtime, required)
        if not missing:
            return button
        if not button:
            break
    raise ValueError(
        "Radar configuration is unavailable; no thresholds were written. "
        "Add/enable the ESPHome LD2410 Query Params button and press it; "
        "if needed use the LD2410 Radar Restart button, then retry. Missing: " + ", ".join(missing)
    )


async def write_threshold(runtime, device_id, key, value, button):
    """Wait between paired-gate transactions and catch rejected/reverted states."""
    entity_id = runtime._find_threshold_entity(device_id, key)
    required = _required_numbers(runtime, device_id, [key])
    if _missing_numbers(runtime, required):
        raise ValueError("Radar configuration became unavailable; stopped before the next write")
    if number_value(runtime.hass.states.get(entity_id)) != value:
        await _call(runtime, "number", "set_value", {"entity_id": entity_id, "value": value})
        await asyncio.sleep(SETTLE_SECONDS)
        await refresh_parameters(runtime, button)
    check_reported(runtime, {key: entity_id}, {key: value})
    return {"ok": True, "entity_id": entity_id, "value": value, "verification": "reported_state"}


def check_reported(runtime, entities, expected):
    """Do not label service-call success as a matching configuration."""
    for key, value in expected.items():
        actual = number_value(runtime.hass.states.get(entities[key]))
        if actual != value:
            raise ValueError(
                f"{key}: requested {value}, device reports {actual:g}; stopped. Query Params and retry"
            )
