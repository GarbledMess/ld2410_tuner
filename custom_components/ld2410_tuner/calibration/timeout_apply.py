"""Learn's radar timeout step. After every Learn (and any automatic Apply of its thresholds),
each radar learned has its timeouts scored at its live thresholds (``timeout_suggestion``,
on the worker as background work). Where automatic timeouts are on for the Learn's source
(``automatic.policy`` kind ``timeout_apply``: a global default, overridden per radar) and the
suggested timeout (the shortest that keeps the chosen share of the best one's improvement)
is not the radar's own, Learn writes it to the radar's timeout number and checks the radar
reports it. The next Learn tunes the thresholds for it."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from time import time

from ..worker import bridge
from . import automatic, device_io
from .timeout_suggestion import KEEP_PERCENT
from .timing_config import read_timing

KIND = "timeout_apply"
_SCALE = {"s": 1.0, "ms": 0.001, "min": 60.0, "h": 3600.0}


def configure_device(runtime, device_id, mode):
    return automatic.configure_device(runtime, device_id, mode, KIND)


def decision(suggestion, current):
    """The timeout to set on the radar, or None when it already has the suggested one (the
    shortest that works nearly as well as the best: ``timeout_suggestion``)."""
    if not suggestion or current is None or suggestion["timeout"] == float(current):
        return None
    return suggestion["timeout"]


def keep(runtime):
    """The share of the best timeout's improvement Learn keeps (learning settings)."""
    saved = runtime.data.get("learning_schedule", {})
    return saved.get("timeout_keep", KEEP_PERCENT) / 100


async def after_learn(runtime, device_ids, report, source):
    """Score (and where permitted set) each radar's timeout; never fails the Learn."""
    found = report.setdefault("timeouts", {})
    for device_id in device_ids:
        entry = await _one(runtime, device_id, source)
        found[device_id] = entry
        runtime.data["devices"][device_id]["timeout_suggestion"] = deepcopy(entry)
        runtime._schedule_save()


async def _one(runtime, device_id, source):
    entry = {"at": time(), "source": source, "status": "unavailable"}
    try:
        timing = read_timing(runtime, device_id)
        current = timing["reported"]["timeout"]
        _entities, live = runtime._threshold_configuration(device_id)
        thresholds = {key: value for key, value in (live or {}).items() if value is not None}
        suggestion = await bridge.device(
            runtime,
            "timeout_suggestion",
            device_id,
            {"thresholds": thresholds, "current": current, "keep": keep(runtime)},
        )
        entry.update(current_timeout=current, suggestion=suggestion)
        if suggestion is None:
            return entry
        target = decision(suggestion, current)
        entry["status"] = "kept" if target is None and current is not None else "suggested"
        if target is None or not automatic.enabled(runtime, source, device_id, KIND):
            return entry
        entity_id = timing["entities"]["timeout"]
        if not entity_id or not entity_id.startswith("number."):
            entry["reason"] = "This radar's timeout cannot be set from Home Assistant."
            return entry
        with device_io.device_operation(runtime, device_id):
            await write(runtime, device_id, entity_id, target)
        entry.update(status="applied", previous_timeout=current, applied_timeout=target)
    except asyncio.CancelledError:
        raise
    except Exception as error:
        entry.update(status="error", reason=str(error))
    return entry


async def write(runtime, device_id, entity_id, seconds):
    """Set the radar's timeout number to ``seconds`` in the number's own unit, then check the
    radar reports it (after Query Params when the radar has the button)."""
    state = runtime.hass.states.get(entity_id)
    attributes = getattr(state, "attributes", {}) or {}
    value = seconds / _SCALE.get(str(attributes.get("unit_of_measurement") or "s"), 1.0)
    low, high = attributes.get("min"), attributes.get("max")
    if (low is not None and value < float(low)) or (high is not None and value > float(high)):
        raise ValueError(f"The radar's timeout cannot be set to {seconds:g} s")
    await device_io._call(runtime, "number", "set_value", {"entity_id": entity_id, "value": value})
    await asyncio.sleep(device_io.SETTLE_SECONDS)
    await device_io.refresh_parameters(runtime, device_io.query_button(runtime, device_id))
    reported = read_timing(runtime, device_id)["reported"]["timeout"]
    if reported != seconds:
        raise ValueError(
            f"timeout: requested {seconds:g} s, device reports {reported}; stopped. "
            "Query Params and retry"
        )
