"""Calibration operations on the shared runtime state."""

from __future__ import annotations

import logging
import math
import time
from copy import deepcopy
from typing import Any

from homeassistant.helpers import entity_registry as er

from .. import entity_index
from ..const import GATE_RE, HISTORY_KEYS
from ..history import policy
from ..rooms import attribution, truth
from . import device_io, jobs, ownership, recovery, results, settings
from . import thresholds as threshold_domain
from .constants import METHOD
from .event_fitting import fit as fit_event_thresholds
from .timing_config import read_timing, timing_signature

_LOGGER = logging.getLogger(__name__)


class LearningEvidenceChanged(ValueError):
    """A fit raced a label edit; a fresh snapshot can be tried."""


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
    entity_id = runtime._find_threshold_entity(device_id, key)
    domain = threshold_domain.from_state(runtime.hass.states.get(entity_id))
    if not domain["min"] <= float(value) <= domain["max"]:
        raise ValueError("Threshold is outside this gate's permitted range")
    value = threshold_domain.normalize(float(value), domain)
    with device_io.device_operation(runtime, device_id):
        button = await device_io.prepare_device(runtime, device_id, [key])
        if domain != threshold_domain.from_state(runtime.hass.states.get(entity_id)):
            raise ValueError("Gate constraints changed while preparing; retry the threshold change")
        return await device_io.write_threshold(runtime, device_id, key, value, button)


async_learn = jobs.async_learn
start_learning = jobs.start_learning


async def _learn_once(runtime, device_id):
    entities, current = await recovery.prepare_learning(runtime, device_id)
    timing = read_timing(runtime, device_id)
    view = attribution.prepared_view(runtime, device_id)
    sources = _learning_sources(runtime, device_id)
    view._fit_timing = timing
    constraints = threshold_domain.read(runtime, entities)
    view._fit_constraints = constraints
    device = runtime.data["devices"][device_id]
    revision = device.get("label_revision", 0)
    jobs.stage(runtime, device_id, "fitting")
    learned = await runtime.hass.async_add_executor_job(
        view._fit_history, device_id, entities, current
    )
    if revision != device.get("label_revision", 0):
        raise LearningEvidenceChanged("Training labels changed while learning; learn again")
    if timing_signature(read_timing(runtime, device_id)) != timing_signature(timing):
        raise ValueError("Device timing changed while learning; learn again")
    if settings.automatic_weight(runtime.data) != settings.automatic_weight(view.data):
        raise LearningEvidenceChanged(
            "Automatic evidence weight changed while learning; learn again"
        )
    if not ownership.intact(view, runtime):
        raise LearningEvidenceChanged("Training history changed while learning; learn again")
    if sources != _learning_sources(runtime, device_id):
        raise LearningEvidenceChanged("Training sources changed while learning; learn again")
    if constraints != threshold_domain.read(runtime, entities):
        raise LearningEvidenceChanged("Gate constraints changed while learning; learn again")
    learned["timing_configuration"] = timing
    learned["label_revision"] = revision
    learned["evidence_sources"] = sources
    return learned


def _learning_sources(runtime, device_id):
    """Configuration and explicit revisions, excluding ordinary sensor transitions."""
    saved = attribution.snapshot(runtime, device_id) or {}
    members = {device_id, *saved.get("members", {})}
    related = truth.related_to(saved.get("groups", {}), saved.get("own", []))
    return deepcopy(
        {
            "groups": saved.get("groups", {}),
            "members": {
                key: {
                    "sources": runtime.data["devices"].get(key, {}).get("presence_sources", {}),
                    "labels": attribution.member_signature(runtime.data["devices"].get(key, {})),
                }
                for key in members
            },
            "rooms": {
                key: {
                    "sources": runtime.data.get("room_presence_sources", {}).get(key, {}),
                    "revision": runtime.data.get("room_label_revision", {}).get(key, 0),
                }
                for key in related
            },
            "retention": policy.settings(runtime.data),
        }
    )


def _fit_history(runtime, device_id, entities, current):
    constraints = getattr(runtime, "_fit_constraints", None)
    if constraints is None:
        constraints = threshold_domain.read(runtime, entities)
    learned = fit_event_thresholds(
        runtime,
        device_id,
        list(entities),
        current,
        getattr(runtime, "_fit_timing", None),
        settings.automatic_weight(runtime.data),
        constraints=constraints,
    )
    attribution.release([runtime])
    learned.update(settings.signature(runtime.data))  # Stale once the setting changes.
    learned["configuration"] = current
    learned["entities"] = entities
    learned["threshold_constraints"] = constraints
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


async def apply(
    runtime, device_id: str, slot=None, result_id=None, expected=None
) -> dict[str, Any]:
    with device_io.device_operation(runtime, device_id):
        try:
            result = await runtime._apply_validated(device_id, slot, result_id, expected)
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


async def _apply_validated(
    runtime, device_id: str, slot=None, result_id=None, expected=None, guard=None
) -> dict[str, Any]:
    device = runtime.data["devices"].get(device_id)
    if not device:
        raise ValueError("Unknown device")
    learned = (
        results.selected_result(device, slot, result_id)
        if slot
        else device.get("last_learning") or {}
    )
    if learned.get("joint"):
        raise ValueError(
            "Apply this complete joint recommendation from Rooms and zones; one radar alone cannot reproduce its result"
        )
    _validate_learning(device, learned, check_revision=slot is None)
    _validate_current_evidence(runtime, device_id, learned)
    button = await device_io.prepare_device(runtime, device_id, learned["entities"])
    _validate_learning(device, learned, check_revision=slot is None)
    _validate_current_evidence(runtime, device_id, learned)
    if guard:
        guard({})
    entities, current = runtime._threshold_configuration(device_id)
    if (
        entities != learned.get("entities")
        or current
        != (
            {key: (expected or {}).get(key) for key in entities}
            if slot
            else learned.get("configuration")
        )
        or set(current) != set(entities)
    ):
        raise ValueError(
            "Threshold configuration changed or is unavailable; learn again before applying"
        )
    # A device receives serial configuration commands. Raise noisy thresholds
    # before lowering sensitive ones and stop on the first partial failure.
    applied, skipped = await _apply_changes(
        runtime, device_id, entities, learned, current, button, slot is None, guard
    )
    _check_completion(guard, applied, skipped)
    if not skipped:
        results.remember_applied(device, learned, entities, current)
    device["last_applied"] = applied
    runtime._schedule_save()
    return {
        "applied": applied,
        "skipped": skipped,
        "verification": "reported_state",
        "note": device_io.READBACK_NOTE,
    }


def _check_completion(guard, applied, skipped):
    if guard and not skipped:
        try:
            guard(applied)
        except ValueError as error:
            skipped["configuration"] = str(error)


def _validate_learning(device, learned, check_revision=True):
    if learned.get("method") != METHOD:
        raise ValueError("Learn thresholds with the current model before applying")
    _validate_proposals(learned)
    if check_revision and learned.get(
        "label_revision", device.get("label_revision", 0)
    ) != device.get("label_revision", 0):
        raise ValueError("Training labels changed; learn again before applying")


def _validate_proposals(learned):
    entities = learned.get("entities") or {}
    proposals = learned.get("proposals") or {}
    if not entities or set(proposals) != set(entities):
        raise ValueError("No complete learned thresholds to apply; learn again")
    for proposal in proposals.values():
        value = proposal.get("threshold") if isinstance(proposal, dict) else None
        if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 100:
            raise ValueError("Invalid learned threshold; learn again")


def _validate_current_evidence(runtime, device_id, learned):
    constraints = threshold_domain.read(runtime, learned["entities"])
    # Changed constraints first: they are why a saved threshold may now be off the grid.
    if learned.get("threshold_constraints", constraints) != constraints:
        raise ValueError("Gate constraints changed; learn again before applying")
    if any(
        proposal["threshold"] not in threshold_domain.allowed(constraints[key])
        for key, proposal in learned["proposals"].items()
    ):
        raise ValueError("Learned threshold is not permitted by this gate; learn again")
    if "evidence_sources" in learned and learned["evidence_sources"] != _learning_sources(
        runtime, device_id
    ):
        raise ValueError("Training sources changed; learn again before applying")
    if learned.get("source") != "device" and any(
        learned.get(key) != value for key, value in settings.signature(runtime.data).items()
    ):
        raise ValueError("Automatic evidence policy changed; learn again before applying")


def _read_threshold_configuration(runtime, device_id, registry, entities, current, limits):
    for entity in entity_index.read(runtime, registry).by_device.get(device_id, ()):
        if entity.domain != "number":
            continue
        value = device_io.number_value(runtime.hass.states.get(entity.entity_id))
        _distance_limit(entity.entity_id, value, limits)
        match = GATE_RE.match(entity.entity_id.split(".", 1)[1])
        if match and match.group("metric") == "threshold":
            key = f"g{match.group('gate')}_{match.group('kind')}"
            entities[key] = entity.entity_id
            if math.isfinite(value):
                current[key] = device_io.threshold_value(value)


def _distance_limit(entity_id, value, limits):
    kind = device_io.distance_kind(entity_id)
    if kind is None:
        return
    if not math.isfinite(value) or not 2 <= value <= 8 or value != int(value):
        raise ValueError("Maximum distance gate is unavailable or outside 2–8")
    limits[kind] = int(value)


async def _apply_changes(
    runtime, device_id, entities, learned, current, button, check_revision=True, guard=None
):
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
            if guard:
                guard(applied)
            _validate_learning(runtime.data["devices"][device_id], learned, check_revision)
            _validate_current_evidence(runtime, device_id, learned)
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
