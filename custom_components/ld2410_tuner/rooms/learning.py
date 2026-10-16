"""One shared fit per learning set, projected into each radar's existing saved-result slots."""

import asyncio
import time
from copy import deepcopy
from uuid import uuid4

from ..calibration import ownership, recovery, results, settings
from ..calibration.constants import METHOD
from ..history import pace
from ..runtime.tasks import finish_task
from . import adaptive, fitting, groups, jobs, truth

# A fit scores the last ``LEARNING_DAYS`` of history. Attribution uses a recent quiet
# baseline: seven days preserved all measured accuracy metrics on the real-data harness
# and shortened linked learning. Set ``ATTRIBUTION_DAYS`` to None for full-history exactness.
LEARNING_DAYS = 7
ATTRIBUTION_DAYS = 7

CHANGED = "Zone labels, settings or membership changed; learn again"


def _radars(chosen):
    return sorted({key for group in chosen.values() for key in group["device_ids"]})


def start_device(runtime, device_id, source, set_id):
    """Join or start the learning set's one fit; return this radar's result task."""
    set_id, name, chosen = groups.learning_set(runtime, set_id)
    radars = _radars(chosen)
    stored = runtime.data.setdefault("room_learning", {}).setdefault(set_id, {})
    if set_id in runtime._room_learning_jobs:
        report = stored["job"]
        if source not in report["sources"]:
            report["sources"].append(source)
        return runtime._learning_jobs[device_id]
    if any(key in runtime._learning_jobs for key in radars):
        raise ValueError("A member radar is already learning; wait for its job to finish")
    report = {
        "id": uuid4().hex,
        "status": "running",
        "stage": "preparing",
        "started_at": time.time(),
        "sources": [source],
        "group_id": set_id,
        "group_name": name,
        "group_ids": list(chosen),
    }
    stored["job"] = report
    task = asyncio.create_task(_run(runtime, set_id, deepcopy(chosen), report))
    runtime._room_learning_jobs[set_id] = task
    task.add_done_callback(lambda done: _job_finished(runtime, set_id, report, done))
    for key in radars:
        runtime.data["devices"][key]["learning_job"] = report
        waiter = asyncio.create_task(_member_result(task, key))
        runtime._learning_jobs[key] = waiter
        waiter.add_done_callback(
            lambda done, member=key: finish_task(runtime._learning_jobs, member, done)
        )
    runtime._schedule_save()
    return runtime._learning_jobs[device_id]


def start_set(runtime, set_id, source="user"):
    """Learn a whole learning set; ``set_id`` may also be one of its group ids."""
    set_id, _name, chosen = groups.learning_set(runtime, set_id)
    start_device(runtime, _radars(chosen)[0], source, set_id)
    return deepcopy(runtime.data["room_learning"][set_id]["job"])


async def _member_result(task, device_id):
    return (await asyncio.shield(task))[device_id]


async def _prepare(runtime, chosen):
    for key in _radars(chosen):
        await recovery.prepare_learning(runtime, key)
    # Resolve through a member zone: the set's id changes when another zone joins it.
    try:
        current = groups.learning_set(runtime, next(iter(chosen)))[2]
    except ValueError:
        current = None
    if current != chosen:
        raise ValueError(CHANGED)
    members, signature = jobs.set_context(runtime, list(chosen))
    since = (
        None
        if ATTRIBUTION_DAYS is None
        else (time.time() - ATTRIBUTION_DAYS * 86400) // 3600 * 3600
    )
    jobs.set_views(runtime, members, since)
    return members, signature, {group_id: truth.view(runtime, group_id) for group_id in chosen}


def _unchanged(runtime, group_ids, signature):
    try:
        current = jobs.set_context(runtime, group_ids)[1]
    except ValueError:
        current = None  # A member or zone disappeared during the fit.
    if current != signature:
        raise ValueError(CHANGED)


async def _run(runtime, set_id, chosen, report):
    try:
        members, signature, rooms = await _prepare(runtime, chosen)
        report["stage"] = "fitting"
        end = time.time()
        async with runtime._comparison_semaphore:
            learned = await jobs.on_views(
                runtime,
                members,
                _fit_set,
                members,
                end - LEARNING_DAYS * 86400,
                end,
                rooms,
                settings.automatic_weight(runtime.data),
            )
            await pace.run(runtime.hass, _check_history, runtime, members)
        _unchanged(runtime, list(chosen), signature)
        learned.update(
            id=report["id"],
            group_id=set_id,
            group_name=report["group_name"],
            group_ids=list(chosen),
            timing={
                key: item["timing"] for found in members.values() for key, item in found.items()
            },
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


def _fit_set(members, start, end, rooms, weight):
    contexts = adaptive.collect_contexts(members, start, end, rooms)
    return fitting.fit_set(members, start, end, rooms, weight, quality_contexts=contexts)


@pace.background
def _check_history(runtime, groups):
    members = {key: item for found in groups.values() for key, item in found.items()}
    if not all(ownership.intact(item["view"], runtime, [key]) for key, item in members.items()):
        raise ValueError(CHANGED)


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


def _measured(learned, device_id):
    """The after-report that scored this radar: the set's own for one zone, otherwise the
    first scored zone it belongs to (the first scored zone if it is only in skipped zones)."""
    if learned["after"].get("room") is not None:
        return learned["after"]
    scored = [item["after"] for item in learned["groups"].values() if "after" in item]
    return next((item for item in scored if device_id in item["timing"]), scored[0])


def projection(learned, device_id):
    signature = learned["signature"][device_id]
    report = _measured(learned, device_id)
    result = {
        "id": learned["id"],
        "method": METHOD,
        "created_at": learned["created_at"],
        "status": learned["status"],
        "configuration": signature["thresholds"],
        "entities": signature["entities"],
        "label_revision": signature["revision"],
        "timing_configuration": learned.get("timing", report["timing"])[device_id],
        "proposals": {
            key: {"threshold": value, "status": learned["status"]}
            for key, value in learned["thresholds"][device_id].items()
        },
        "joint": {
            "group_id": learned["group_id"],
            "name": learned["group_name"],
            "device_ids": sorted(learned["thresholds"]),
            "report": {
                field: report[field]
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
    if "automatic_evidence" in learned:
        result["automatic_evidence"] = deepcopy(learned["automatic_evidence"])
    return result


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
