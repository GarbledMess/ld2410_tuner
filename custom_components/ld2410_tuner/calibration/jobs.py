"""Server-owned learning tasks and bounded, reconnectable job reports."""

import asyncio
import logging
from copy import deepcopy
from time import time
from uuid import uuid4

from . import results

_LOGGER = logging.getLogger(__name__)


def start(runtime, device_id, source="user"):
    """Join one fit per device; the task owns saving every requested result slot."""
    if source not in ("user", "automatic"):
        raise ValueError("Unknown learning source")
    device = runtime.data["devices"].get(device_id)
    if device is None:
        raise ValueError("Unknown device")
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
        saved = {
            source: results.remember_learning(device, learned, source)
            for source in report["sources"]
        }
        report.update(
            status="completed",
            results={source: result["id"] for source, result in saved.items()},
        )
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


def _finished(runtime, device_id, report, task):
    # Cancellation before the coroutine starts cannot reach its finally block.
    if task.cancelled() and report["status"] == "running":
        report.update(
            status="interrupted",
            error="Learning interrupted by integration shutdown",
            finished_at=time(),
        )
        runtime._schedule_save()
    if runtime._learning_jobs.get(device_id) is task:
        runtime._learning_jobs.pop(device_id)
    # A browser may leave without awaiting the job; its error is in the saved report.
    if not task.cancelled():
        task.exception()
