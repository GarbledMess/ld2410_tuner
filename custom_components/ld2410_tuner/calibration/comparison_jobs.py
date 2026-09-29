"""Deduplicated score jobs; completed versioned assessments live in device storage."""

import asyncio
import time
from copy import deepcopy

from ..history import policy
from . import comparison, comparison_cache, results
from .constants import METHOD
from .timing_config import read_timing, timing_signature


def _pattern(result, entities):
    if not result:
        return None
    return {
        "id": result.get("id"),
        "thresholds": {
            key: value.get("threshold")
            for key, value in result.get("proposals", {}).items()
            if isinstance(value, dict)
        },
        "applicable": result.get("method") == METHOD and result.get("entities") == entities,
    }


def _context(runtime, device_id):
    device = runtime.data["devices"].get(device_id)
    if device is None:
        raise ValueError("Unknown device")
    entities, current = runtime._threshold_configuration(device_id)
    saved = results.saved_results(device)
    timing = read_timing(runtime, device_id)
    patterns = {slot: _pattern(saved.get(slot), entities) for slot in results.SLOTS}
    patterns["live"] = {"id": None, "thresholds": current, "applicable": False}
    context = {"keys": list(entities), "entities": entities, "patterns": patterns, "timing": timing}
    signature = {
        "scorer_version": comparison.SCORER_VERSION,
        "entities": entities,
        "patterns": patterns,
        "timing": timing_signature(timing),
        "revision": device.get("label_revision", 0),
        "training": [
            device.get(key)
            for key in ("training_state", "training_label_start", "training_expires_at")
        ],
        "retention": policy.settings(runtime.data),
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
        task.add_done_callback(lambda done: _finished(runtime, device_id, done))
    return await asyncio.shield(task)


async def _run(runtime, device_id, context, signature, wanted):
    async with runtime._comparison_semaphore:
        started = time.time()
        try:
            view = runtime._history_view(device_id)
            report = await runtime.hass.async_add_executor_job(
                comparison.calculate, view, device_id, {**context, "patterns": wanted}
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


def _finished(runtime, device_id, task):
    if runtime._comparison_jobs.get(device_id) is task:
        runtime._comparison_jobs.pop(device_id, None)
    if not task.cancelled():
        task.exception()
