"""One shared joint fit, projected into each radar's existing saved-result slots."""

import asyncio
import time
from copy import deepcopy
from uuid import uuid4

from ..calibration import recovery, results
from ..calibration.constants import METHOD
from ..runtime.tasks import finish_task
from . import fitting, groups, jobs


def start_device(runtime, device_id, source, group_id):
    group = groups.learning_group(runtime, group_id)
    stored = runtime.data.setdefault("room_learning", {}).setdefault(group_id, {})
    if group_id in runtime._room_learning_jobs:
        report = stored["job"]
        if source not in report["sources"]:
            report["sources"].append(source)
        return runtime._learning_jobs[device_id]
    if any(key in runtime._learning_jobs for key in group["device_ids"]):
        raise ValueError("A member radar is already learning; wait for its job to finish")
    report = {
        "id": uuid4().hex,
        "status": "running",
        "stage": "preparing",
        "started_at": time.time(),
        "sources": [source],
        "group_id": group_id,
        "group_name": group["name"],
    }
    stored["job"] = report
    task = asyncio.create_task(_run(runtime, group_id, deepcopy(group), report))
    runtime._room_learning_jobs[group_id] = task
    task.add_done_callback(lambda done: _job_finished(runtime, group_id, report, done))
    for key in group["device_ids"]:
        runtime.data["devices"][key]["learning_job"] = report
        waiter = asyncio.create_task(_member_result(task, key))
        runtime._learning_jobs[key] = waiter
        waiter.add_done_callback(
            lambda done, member=key: finish_task(runtime._learning_jobs, member, done)
        )
    runtime._schedule_save()
    return runtime._learning_jobs[device_id]


def start(runtime, group_id):
    group = groups.learning_group(runtime, group_id)
    start_device(runtime, group["device_ids"][0], "user", group_id)
    return deepcopy(runtime.data["room_learning"][group_id]["job"])


async def _member_result(task, device_id):
    return (await asyncio.shield(task))[device_id]


async def _prepare(runtime, group_id, group):
    for key in group["device_ids"]:
        await recovery.prepare_learning(runtime, key)
    members, signature = jobs._context(runtime, group_id)
    if signature["group"] != group:
        raise ValueError("Zone membership changed; learn again")
    for key, item in members.items():
        item["view"] = runtime._history_view(key)
    return members, signature


async def _run(runtime, group_id, group, report):
    try:
        members, signature = await _prepare(runtime, group_id, group)
        report["stage"] = "fitting"
        end = time.time()
        async with runtime._comparison_semaphore:
            learned = await runtime.hass.async_add_executor_job(
                fitting.fit, members, end - 7 * 86400, end
            )
        if jobs._context(runtime, group_id)[1] != signature:
            raise ValueError("Zone labels, settings or membership changed; learn again")
        learned.update(
            id=report["id"],
            group_id=group_id,
            group_name=group["name"],
            signature=signature,
            created_at=end,
        )
        saved = _save(runtime, learned, report)
        await _manual_apply(runtime, learned, report)
        saved = _save(runtime, learned, report, saved)
        report.update(status="completed", results=dict.fromkeys(report["sources"], learned["id"]))
        return saved
    except asyncio.CancelledError:
        report.update(
            status="interrupted", error="Joint learning interrupted by integration shutdown"
        )
        raise
    except Exception as error:
        report.update(status="error", error=str(error))
        raise
    finally:
        report["finished_at"] = time.time()
        runtime._schedule_save()


async def _manual_apply(runtime, learned, report):
    if "user" not in report["sources"]:
        return
    from . import application

    report["automatic_apply"] = {}
    await application.automatic(runtime, learned, report["automatic_apply"], "user")


def _save(runtime, learned, report, previous=None):
    stored = runtime.data["room_learning"][learned["group_id"]].setdefault("slots", {})
    saved = previous or {}
    for key in learned["thresholds"]:
        device = runtime.data["devices"][key]
        remembered = saved.setdefault(key, {})
        _save_member(device, learned, key, report["sources"], remembered)
    for source in report["sources"]:
        stored[source] = learned
    return saved


def projection(learned, device_id):
    signature = learned["signature"][device_id]
    return {
        "id": learned["id"],
        "method": METHOD,
        "created_at": learned["created_at"],
        "status": learned["status"],
        "configuration": signature["thresholds"],
        "entities": signature["entities"],
        "label_revision": signature["revision"],
        "timing_configuration": learned["after"]["timing"][device_id],
        "proposals": {
            key: {"threshold": value, "status": learned["status"]}
            for key, value in learned["thresholds"][device_id].items()
        },
        "joint": {
            "group_id": learned["group_id"],
            "name": learned["group_name"],
            "device_ids": list(learned["thresholds"]),
            "report": {
                field: learned["after"][field]
                for field in (
                    "room",
                    "start",
                    "end",
                    "scorer_version",
                    "excluded",
                    "shared_recording_seconds",
                )
            },
        },
        "warnings": [
            "Joint zone result: all member radars are required. Review and apply the complete recommendation in Rooms and zones."
        ],
    }


def _save_member(device, learned, key, sources, remembered):
    for source, result in remembered.items():
        if results.saved_results(device).get(source, {}).get("id") != result["id"]:
            raise ValueError("Joint recommendations were cleared or replaced during learning")
    for source in sources:
        if source not in remembered:
            remembered[source] = results.remember_learning(device, projection(learned, key), source)


def _job_finished(runtime, group_id, report, task):
    if task.cancelled() and report["status"] == "running":
        report.update(
            status="interrupted",
            error="Joint learning interrupted by integration shutdown",
            finished_at=time.time(),
        )
        runtime._schedule_save()
    finish_task(runtime._room_learning_jobs, group_id, task)
