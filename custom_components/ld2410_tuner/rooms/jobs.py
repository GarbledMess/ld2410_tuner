"""Server-owned assessment jobs with persisted, versioned results."""

import asyncio
import math
import time
from copy import deepcopy

from ..calibration.timing_config import read_timing, timing_signature
from ..runtime.tasks import finish_task
from . import assessment, groups


def _context(runtime, group_id):
    _, available, definitions = groups.definitions(runtime)
    group = definitions.get(group_id)
    if not group:
        raise ValueError("This group no longer exists")
    members, signature = {}, {"group": group, "scorer_version": assessment.SCORER_VERSION}
    for key in group["device_ids"]:
        device = runtime.data["devices"].get(key)
        if not device:
            raise ValueError("A group member is no longer available; edit its membership")
        name = available.get(key, {}).get("name", key)
        try:
            entities, thresholds = runtime._threshold_configuration(key)
        except ValueError as error:
            raise ValueError(f"{name}: {error}") from error
        if not thresholds or set(thresholds) != set(entities):
            raise ValueError(
                f"{name}: threshold settings are missing; restore them before assessing"
            )
        timing = read_timing(runtime, key)
        members[key] = {"thresholds": thresholds, "timing": timing}
        signature[key] = {
            "entities": entities,
            "thresholds": thresholds,
            "timing": timing_signature(timing),
            "revision": device.get("label_revision", 0),
            "training": [
                device.get(field)
                for field in ("training_state", "training_label_start", "training_expires_at")
            ],
        }
    return members, signature


def summary(runtime, group_id):
    saved = runtime.data.get("room_assessments", {}).get(group_id, {})
    if group_id in runtime._room_jobs:
        return {**saved.get("report", {}), "state": "running"}
    if not saved:
        return {"state": "pending"}
    try:
        _, signature = _context(runtime, group_id)
    except ValueError as error:
        return {**saved["report"], "state": "stale", "reason": str(error)}
    if signature != saved["signature"]:
        return {
            **saved["report"],
            "state": "stale",
            "reason": "Membership, labels, thresholds or timing changed. Assess again.",
        }
    return saved["report"]


def snapshot(runtime):
    areas, members, definitions = groups.definitions(runtime)
    return {
        "areas": areas,
        "members": members,
        "groups": {
            key: {
                **item,
                "assessment": summary(runtime, key),
                "learning": runtime.data.get("room_learning", {}).get(key),
            }
            for key, item in definitions.items()
        },
    }


def start(runtime, group_id, hours=24):
    if type(hours) not in (int, float) or not math.isfinite(hours) or not 1 <= hours <= 168:
        raise ValueError("Choose between 1 and 168 hours of history")
    if group_id not in runtime._room_jobs:
        members, signature = _context(runtime, group_id)
        task = runtime.hass.async_create_task(
            _run(runtime, group_id, members, deepcopy(signature), hours)
        )
        runtime._room_jobs[group_id] = task
        task.add_done_callback(lambda done: finish_task(runtime._room_jobs, group_id, done))
    return {"state": "running"}


async def _run(runtime, group_id, members, signature, hours):
    async with runtime._comparison_semaphore:
        end = time.time()
        try:
            for key, item in members.items():
                item["view"] = runtime._history_view(key)
            report = await runtime.hass.async_add_executor_job(
                assessment.calculate, members, end - hours * 3600, end
            )
            report.update(state="ready", evaluated_at=end)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            report = {"state": "error", "reason": str(error), "evaluated_at": end}
        _save_report(runtime, group_id, signature, report)


def _save_report(runtime, group_id, signature, report):
    if group_id not in groups.definitions(runtime)[2]:
        return  # A removed group must not be resurrected by a finishing job.
    try:
        _, current = _context(runtime, group_id)
        if signature != current:
            report = {
                "state": "stale",
                "reason": "Group settings or labels changed during assessment. Assess again.",
            }
    except ValueError as error:
        report = {"state": "stale", "reason": str(error)}
    runtime.data.setdefault("room_assessments", {})[group_id] = {
        "signature": signature,
        "report": report,
    }
    runtime._schedule_save()
