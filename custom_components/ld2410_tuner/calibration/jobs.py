"""Server-owned learning tasks and bounded, reconnectable job reports."""

import asyncio
import logging
from copy import deepcopy
from time import time
from uuid import uuid4

from ..runtime.tasks import finish_task
from . import automatic, results

_LOGGER = logging.getLogger(__name__)


def start(runtime, device_id, source="user"):
    """Join one fit per device; the task owns saving every requested result slot."""
    if source not in ("user", "automatic"):
        raise ValueError("Unknown learning source")
    device = runtime.data["devices"].get(device_id)
    if device is None:
        raise ValueError("Unknown device")
    from ..rooms import groups, learning

    group_id = groups.for_device(runtime, device_id)
    if group_id:
        return learning.start_device(runtime, device_id, source, group_id)
    task = runtime._learning_jobs.get(device_id)
    if task is None:
        report = {
            "id": uuid4().hex,
            "status": "running",
            "stage": "preparing",
            "started_at": time(),
            "sources": [source],
        }
        device["learning_job"] = report
        task = asyncio.create_task(_run(runtime, device_id, device, report))
        runtime._learning_jobs[device_id] = task
        task.add_done_callback(lambda done: _finished(runtime, device_id, report, done))
    elif source not in device["learning_job"]["sources"]:
        device["learning_job"]["sources"].append(source)
    runtime._schedule_save()
    return task


def start_learning(runtime, device_id):
    """Acknowledge acceptance immediately; snapshots provide progress and results."""
    start(runtime, device_id)
    return deepcopy(runtime.data["devices"][device_id]["learning_job"])


async def async_learn(runtime, device_id, source="user"):
    """Compatibility API for callers such as the overnight scheduler."""
    saved = await asyncio.shield(start(runtime, device_id, source))
    return saved[source]


def stage(runtime, device_id, value):
    report = runtime.data["devices"][device_id].get("learning_job")
    if report and report["status"] == "running":
        report["stage"] = value
        runtime._schedule_save()


async def _run(runtime, device_id, device, report):
    try:
        learned = await runtime._learn_once(device_id)
        stage(runtime, device_id, "saving")
        saved = {}
        _save_results(device, report, learned, saved)
        await _apply_manual_result(runtime, device_id, report, saved)
        # Another caller may join while comparison or writes are awaiting I/O.
        _save_results(device, report, learned, saved)
        report["status"] = "completed"
        return saved
    except asyncio.CancelledError:
        report.update(status="interrupted", error="Learning interrupted by integration shutdown")
        raise
    except Exception as error:
        report.update(status="error", error=str(error))
        _LOGGER.warning("Learning failed for device %s: %s", device_id, error)
        raise
    finally:
        report["finished_at"] = time()
        runtime._learning_jobs.pop(device_id, None)
        runtime._schedule_save()


def _save_results(device, report, learned, saved):
    current = device.get("learning_results", {})
    if any(current.get(source, {}).get("id") != result["id"] for source, result in saved.items()):
        raise ValueError("Saved learning was cleared or replaced while the job was running")
    for source in report["sources"]:
        if source not in saved:
            saved[source] = results.remember_learning(device, learned, source)
    report["results"] = {source: result["id"] for source, result in saved.items()}


async def _apply_manual_result(runtime, device_id, report, saved):
    if "user" not in saved or not automatic.enabled(runtime, "user", device_id):
        return
    stage(runtime, device_id, "automatic_apply")
    report["automatic_apply"] = {}
    await automatic.run(runtime, device_id, saved["user"], report["automatic_apply"], "user")


def _finished(runtime, device_id, report, task):
    # Cancellation before the coroutine starts cannot reach its finally block.
    if task.cancelled() and report["status"] == "running":
        report.update(
            status="interrupted",
            error="Learning interrupted by integration shutdown",
            finished_at=time(),
        )
        runtime._schedule_save()
    finish_task(runtime._learning_jobs, device_id, task)
