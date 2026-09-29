"""Deduplicated read-only comparison jobs and per-device in-memory result cache."""

import asyncio
import time
from copy import deepcopy

from ..history import policy
from . import comparison, results
from .constants import METHOD
from .timing_config import read_timing, timing_signature

REFRESH_SECONDS = 60


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
    context = {"keys": list(entities), "patterns": patterns, "timing": timing}
    signature = {
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


def _history_token(runtime, device_id):
    history = runtime.data["devices"][device_id].get("history", [])
    pending = runtime._history_runtime.get(device_id, {}).get("samples", [])
    return (
        len(history),
        sum(block.get("count", 0) for block in history),
        history[0].get("start") if history else None,
        history[-1].get("start") if history else None,
        len(pending),
        pending[-1][0] if pending else None,
        int(time.time() // 300),
    )


def summary(runtime, device_id):
    try:
        _context_data, signature = _context(runtime, device_id)
    except ValueError as error:
        return {"state": "unavailable", "reason": str(error)}
    cached = runtime._comparison_cache.get(device_id)
    running = device_id in runtime._comparison_jobs
    if cached is None or cached["signature"] != signature:
        return {"state": "running" if running else "pending"}
    old = time.time() - cached["finished_at"] >= REFRESH_SECONDS
    changed = cached["history"] != _history_token(runtime, device_id)
    state = "running" if running else "pending" if old and changed else cached["state"]
    return {**cached["report"], "state": state, "stale": old and changed}


async def compare_results(runtime, device_id, force=False):
    context, signature = _context(runtime, device_id)
    task = runtime._comparison_jobs.get(device_id)
    if task is None:
        report = summary(runtime, device_id)
        if not force and report["state"] not in ("pending", "running"):
            return report
        task = asyncio.create_task(_run(runtime, device_id, context, signature))
        runtime._comparison_jobs[device_id] = task
        task.add_done_callback(lambda done: _finished(runtime, device_id, done))
    return await asyncio.shield(task)


async def _run(runtime, device_id, context, signature):
    async with runtime._comparison_semaphore:
        history = _history_token(runtime, device_id)
        started = time.time()
        try:
            view = runtime._history_view(device_id)
            report = await runtime.hass.async_add_executor_job(
                comparison.calculate, view, device_id, context
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
        runtime._comparison_cache[device_id] = {
            "signature": deepcopy(signature),
            "history": history,
            "finished_at": time.time(),
            "state": report["state"],
            "report": report,
        }
        return report


def _finished(runtime, device_id, task):
    if runtime._comparison_jobs.get(device_id) is task:
        runtime._comparison_jobs.pop(device_id, None)
    if not task.cancelled():
        task.exception()
