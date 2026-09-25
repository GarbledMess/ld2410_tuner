"""Calibration operations on the shared runtime state."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import deque
from typing import Any

from homeassistant.helpers import entity_registry as er

from ..const import GATE_RE, HISTORY_KEYS
from ..history.labels import _history_label_reader
from . import device_io
from .fitting import MAX_CLASS_SAMPLES, METHOD, MIN_AUTO_CONFIDENCE, fit_thresholds

_LOGGER = logging.getLogger(__name__)


def _find_threshold_entity(runtime, device_id: str, key: str) -> str:
    registry = er.async_get(runtime.hass)
    for entity in registry.entities.values():
        if entity.domain != "number" or entity.device_id != device_id:
            continue
        match = GATE_RE.match(entity.entity_id.split(".", 1)[1])
        if (
            match
            and match.group("metric") == "threshold"
            and f"g{match.group('gate')}_{match.group('kind')}" == key
        ):
            return entity.entity_id
    raise ValueError("No matching threshold entity found for that gate")


async def set_gate_threshold(runtime, device_id: str, key: str, value: float) -> dict[str, Any]:
    if key not in HISTORY_KEYS:
        raise ValueError("Unknown gate key")
    if not math.isfinite(float(value)) or not 0 <= float(value) <= 100:
        raise ValueError("Threshold must be between 0 and 100")
    value = round(float(value))
    with device_io.device_operation(runtime, device_id):
        button = await device_io.prepare_device(runtime, device_id, [key])
        return await device_io.write_threshold(runtime, device_id, key, value, button)


async def async_learn(runtime, device_id):
    if device_id not in runtime._learning_jobs:
        runtime._learning_jobs[device_id] = asyncio.create_task(runtime._learn_once(device_id))
    return await asyncio.shield(runtime._learning_jobs[device_id])


async def _learn_once(runtime, device_id):
    try:
        entities, current = runtime._threshold_configuration(device_id)
        view = runtime._history_view(device_id)
        device = runtime.data["devices"][device_id]
        revision = device.get("label_revision", 0)
        learned = await runtime.hass.async_add_executor_job(
            view._fit_history, device_id, entities, current
        )
        if revision != device.get("label_revision", 0):
            raise ValueError("Training labels changed while learning; learn again")
        learned["label_revision"] = revision
        device["last_learning"] = learned
        runtime._schedule_save()
        return learned
    finally:
        runtime._learning_jobs.pop(device_id, None)


def _fit_history(runtime, device_id, entities, current):
    device = runtime.data["devices"][device_id]
    groups = {label: deque(maxlen=MAX_CLASS_SAMPLES) for label in ("present", "not_present")}
    automatic = {label: deque(maxlen=MAX_CLASS_SAMPLES) for label in groups}
    indices = [HISTORY_KEYS.index(key) for key in entities]
    label_at = _history_label_reader(device)
    for ts, row in runtime._iter_history_samples(device, include_auto=True):
        if not any(row[index] <= 100 for index in indices):
            continue
        label = label_at(ts)
        if label in groups:
            groups[label].append((ts, row, label))
        elif label is None and len(row) >= len(HISTORY_KEYS) + 2:
            _append_auto_sample(automatic, ts, row)

    def values(row):
        return {key: row[i] for i, key in enumerate(HISTORY_KEYS) if row[i] <= 100}

    rows = [(ts, values(row), label) for group in groups.values() for ts, row, label in group]
    guesses = [
        (ts, values(row), label, confidence)
        for group in automatic.values()
        for ts, row, label, confidence in group
    ]
    learned = fit_thresholds(rows, list(entities), current, guesses)
    learned["configuration"] = current
    learned["entities"] = entities
    learned["created_at"] = time.time()
    return learned


def _threshold_configuration(runtime, device_id):
    entities, current, limits = {}, {}, {}
    registry = er.async_get(runtime.hass)
    _read_threshold_configuration(runtime, device_id, registry, entities, current, limits)
    entities = {
        key: entity_id
        for key, entity_id in entities.items()
        if int(key[1]) <= limits.get(key.split("_")[1], 8)
    }
    expected = {
        f"g{gate}_{kind}" for kind in ("move", "still") for gate in range(limits.get(kind, 8) + 1)
    }
    missing = expected - set(entities)
    if missing:
        raise ValueError(
            "Enable all active gate threshold entities before learning: "
            + ", ".join(sorted(missing))
        )
    current = {key: value for key, value in current.items() if key in entities}
    return entities, current


async def apply(runtime, device_id: str) -> dict[str, Any]:
    with device_io.device_operation(runtime, device_id):
        try:
            result = await runtime._apply_validated(device_id)
        except ValueError as error:
            _LOGGER.warning("Apply rejected before threshold writes: %s", error)
            raise
        if result["skipped"]:
            _LOGGER.warning(
                "Apply incomplete: %d reported matches; %s",
                len(result["applied"]),
                result["skipped"],
            )
        else:
            _LOGGER.info(
                "Apply completed with %d reported matches (not hardware acknowledgements)",
                len(result["applied"]),
            )
        return result


async def _apply_validated(runtime, device_id: str) -> dict[str, Any]:
    device = runtime.data["devices"].get(device_id)
    if not device:
        raise ValueError("Unknown device")
    learned = device.get("last_learning") or {}
    _validate_learning(device, learned)
    button = await device_io.prepare_device(runtime, device_id, learned["entities"])
    _validate_learning(device, learned)
    entities, current = runtime._threshold_configuration(device_id)
    if (
        entities != learned.get("entities")
        or current != learned.get("configuration")
        or set(current) != set(entities)
    ):
        raise ValueError(
            "Threshold configuration changed or is unavailable; learn again before applying"
        )
    # A device receives serial configuration commands. Raise noisy thresholds
    # before lowering sensitive ones and stop on the first partial failure.
    applied, skipped = await _apply_changes(runtime, device_id, entities, learned, current, button)
    device["last_applied"] = applied
    runtime._schedule_save()
    return {
        "applied": applied,
        "skipped": skipped,
        "verification": "reported_state",
        "note": device_io.READBACK_NOTE,
    }


def _validate_learning(device, learned):
    if learned.get("method") != METHOD or learned.get("status") != "ok":
        raise ValueError("Learn thresholds with the current model before applying")
    if learned.get("label_revision", device.get("label_revision", 0)) != device.get(
        "label_revision", 0
    ):
        raise ValueError("Training labels changed; learn again before applying")


def _read_threshold_configuration(runtime, device_id, registry, entities, current, limits):
    for entity in registry.entities.values():
        if entity.device_id != device_id or entity.domain != "number":
            continue
        value = device_io.number_value(runtime.hass.states.get(entity.entity_id))
        _distance_limit(entity.entity_id, value, limits)
        match = GATE_RE.match(entity.entity_id.split(".", 1)[1])
        if match and match.group("metric") == "threshold":
            key = f"g{match.group('gate')}_{match.group('kind')}"
            entities[key] = entity.entity_id
            if math.isfinite(value):
                current[key] = value


def _distance_limit(entity_id, value, limits):
    kind = device_io.distance_kind(entity_id)
    if kind is None:
        return
    if not math.isfinite(value) or not 2 <= value <= 8 or value != int(value):
        raise ValueError("Maximum distance gate is unavailable or outside 2–8")
    limits[kind] = int(value)


async def _apply_changes(runtime, device_id, entities, learned, current, button):
    changes = sorted(
        entities,
        key=lambda key: learned["proposals"][key]["threshold"] - current[key],
        reverse=True,
    )
    applied, skipped = {}, {}
    failed = False
    for key in changes:
        value = learned["proposals"][key]["threshold"]
        if failed:
            skipped[key] = "Not attempted after an earlier write failed"
            continue
        try:
            _validate_learning(runtime.data["devices"][device_id], learned)
            await device_io.write_threshold(runtime, device_id, key, value, button)
            device_io.check_reported(runtime, entities, applied)
            applied[key] = value
        except Exception as err:
            skipped[key] = str(err)
            failed = True
    # A later paired write or delayed query can undo an earlier optimistic echo.
    for key in applied.copy():
        try:
            device_io.check_reported(runtime, entities, {key: applied[key]})
        except ValueError as error:
            applied.pop(key)
            skipped[key] = str(error)
    return applied, skipped


def _append_auto_sample(automatic, ts, row):
    inferred = {1: "present", 2: "not_present"}.get(row[len(HISTORY_KEYS)])
    confidence = row[len(HISTORY_KEYS) + 1] / 100
    if inferred and confidence >= MIN_AUTO_CONFIDENCE:
        automatic[inferred].append((ts, row, inferred, confidence))
