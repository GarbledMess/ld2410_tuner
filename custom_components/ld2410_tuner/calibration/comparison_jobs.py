"""Deduplicated score jobs; completed versioned assessments live in device storage."""

import asyncio
import time
from copy import deepcopy

from ..const import TRAINING_FIELDS
from ..history import pace, policy
from ..rooms import attribution
from ..runtime.tasks import finish_task
from . import comparison, comparison_cache, results, settings
from . import thresholds as threshold_domain
from .constants import METHOD
from .timing_config import read_timing, timing_signature


def _pattern(result, entities, constraints=None, learner_policy=None):
    if not result:
        return None
    values = {
        key: value.get("threshold")
        for key, value in result.get("proposals", {}).items()
        if isinstance(value, dict)
    }
    legal = constraints is None or threshold_domain.permitted(values, constraints)
    compatible = (
        constraints is None or result.get("threshold_constraints", constraints) == constraints
    )
    policy_compatible = (
        learner_policy is None
        or result.get("source") == "device"
        or all(result.get(key) == value for key, value in learner_policy.items())
    )
    return {
        "id": result.get("id"),
        "joint": result.get("joint"),
        "thresholds": values,
        "applicable": result.get("method") == METHOD
        and result.get("entities") == entities
        and legal
        and compatible
        and policy_compatible,
    }


def _context(runtime, device_id):
    device = runtime.data["devices"].get(device_id)
    if device is None:
        raise ValueError("Unknown device")
    entities, current = runtime._threshold_configuration(device_id)
    saved = results.saved_results(device)
    timing = read_timing(runtime, device_id)
    constraints = threshold_domain.read(runtime, entities)
    learner_policy = settings.signature(runtime.data)
    patterns = {
        slot: _pattern(saved.get(slot), entities, constraints, learner_policy)
        for slot in results.SLOTS
    }
    patterns["live"] = {"id": None, "thresholds": current, "applicable": False}
    context = {
        "keys": list(entities),
        "entities": entities,
        "patterns": patterns,
        "timing": timing,
        **learner_policy,
        "threshold_constraints": constraints,
    }
    signature = {
        "scorer_version": comparison.SCORER_VERSION,
        "entities": entities,
        "patterns": patterns,
        "timing": timing_signature(timing),
        "revision": device.get("label_revision", 0),
        "training": [device.get(key) for key in TRAINING_FIELDS],
        "retention": policy.settings(runtime.data),
        "learner_policy": learner_policy,
        "threshold_constraints": constraints,
    }
    return context, signature


def summary(runtime, device_id):
    try:
        context, signature = _context(runtime, device_id)
    except ValueError as error:
        return {"state": "unavailable", "reason": str(error)}
    report = comparison_cache.report(runtime.data["devices"][device_id], context)
    if device_id in runtime._comparison_jobs:
        return {**report, "state": "running"}
    failed = runtime._comparison_cache.get(device_id)
    if failed and failed["signature"] == signature:
        return {**report, **failed["report"]}
    return report


async def compare_results(runtime, device_id, force=False):
    context, signature = _context(runtime, device_id)
    task = runtime._comparison_jobs.get(device_id)
    if task is None:
        device = runtime.data["devices"][device_id]
        wanted = comparison_cache.missing(device, context, retry_unscored=bool(force))
        report = summary(runtime, device_id)
        if not wanted or (not force and report["state"] == "error"):
            return report
        task = asyncio.create_task(_run(runtime, device_id, context, signature, wanted))
        runtime._comparison_jobs[device_id] = task
        task.add_done_callback(lambda done: finish_task(runtime._comparison_jobs, device_id, done))
    return await asyncio.shield(task)


async def _run(runtime, device_id, context, signature, wanted):
    async with runtime._comparison_semaphore:
        started = time.time()
        try:
            view = attribution.prepared_view(runtime, device_id)
            report = await pace.run(
                runtime.hass, comparison.calculate, view, device_id, {**context, "patterns": wanted}
            )
            report.update(state="ready", evaluated_at=started)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            report = {"state": "error", "reason": str(error), "evaluated_at": started}
        try:
            _, current = _context(runtime, device_id)
        except ValueError:
            return {"state": "pending"}
        if current != signature:
            return {"state": "pending"}
        if report["state"] == "error":
            runtime._comparison_cache[device_id] = {
                "signature": deepcopy(signature),
                "report": report,
            }
            return report
        device = runtime.data["devices"][device_id]
        comparison_cache.remember(device, context, report)
        runtime._comparison_cache.pop(device_id, None)
        runtime._schedule_save()
        return comparison_cache.report(device, context)
