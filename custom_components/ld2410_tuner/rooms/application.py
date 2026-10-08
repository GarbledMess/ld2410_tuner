"""Apply a complete joint recommendation through the existing paced radar writer."""

import asyncio
from contextlib import ExitStack
from copy import deepcopy
from time import time

from ..calibration import automatic as policy
from ..calibration import device_io, results
from ..calibration.scoring import improves
from . import fitting, groups, jobs
from .objective import Objective


def selected(runtime, group_id, result_id, source):
    learned = runtime.data.get("room_learning", {}).get(group_id, {}).get("slots", {}).get(source)
    if not learned or learned["id"] != result_id or learned["method"] != fitting.METHOD:
        raise ValueError("Joint result was replaced or removed; learn the zone again")
    return learned


def guard(runtime, learned, applied, source=None):
    group_id = learned["group_id"]
    groups.learning_group(runtime, group_id)
    current = jobs._context(runtime, group_id)[1]
    expected = deepcopy(learned["signature"])
    for key, thresholds in applied.items():
        expected[key]["thresholds"].update(thresholds)
    if current != expected:
        raise ValueError(
            "A zone member's labels, timing, thresholds or membership changed; learn again"
        )
    for key in learned["thresholds"]:
        if source and not policy.enabled(runtime, source, key):
            raise ValueError("Automatic Apply must be enabled for this source on every zone member")
        saved = results.saved_results(runtime.data["devices"][key])
        if not any(item.get("id") == learned["id"] for item in saved.values()):
            raise ValueError("A member's saved recommendation was cleared or replaced; learn again")


def _changes(learned):
    changes = [
        (value - learned["signature"][device]["thresholds"][key], device, key, value)
        for device, gates in learned["thresholds"].items()
        for key, value in gates.items()
    ]
    # Establish replacement coverage before withdrawing the previous coverage.
    return sorted(changes)


async def _write(runtime, learned, source):
    applied = {key: {} for key in learned["thresholds"]}
    skipped, buttons = {}, {}
    guard(runtime, learned, applied, source)
    for key in applied:
        buttons[key] = await device_io.prepare_device(runtime, key, learned["thresholds"][key])
    guard(runtime, learned, applied, source)
    for _delta, device, key, value in _changes(learned):
        try:
            guard(runtime, learned, applied, source)
            await device_io.write_threshold(runtime, device, key, value, buttons[device])
            applied[device][key] = value
        except Exception as error:
            skipped[f"{device}.{key}"] = str(error)
            break
    try:
        guard(runtime, learned, applied, source)
    except ValueError as error:
        skipped["configuration"] = str(error)
    if not skipped:
        _remember(runtime, learned)
    return {
        "applied": applied,
        "skipped": skipped,
        "verification": "reported_state",
        "note": device_io.READBACK_NOTE,
    }


def _remember(runtime, learned):
    from .learning import projection

    for key in learned["thresholds"]:
        device = runtime.data["devices"][key]
        previous = learned["signature"][key]
        results.remember_applied(
            device, projection(learned, key), previous["entities"], previous["thresholds"]
        )
        device["last_applied"] = dict(learned["thresholds"][key])


async def apply(runtime, group_id, result_id, source="user", automatic_source=None):
    learned = selected(runtime, group_id, result_id, source)
    stored = runtime.data["room_learning"][group_id]
    report = {"status": "applying", "result_id": result_id}
    stored["application"] = report
    try:
        with ExitStack() as stack:
            for key in sorted(learned["thresholds"]):
                stack.enter_context(device_io.device_operation(runtime, key))
            result = await _write(runtime, learned, automatic_source)
        report.update(status="partial" if result["skipped"] else "applied", writes=result)
        return result
    except asyncio.CancelledError:
        report.update(
            status="interrupted", reason="Joint Apply interrupted; check all member thresholds"
        )
        raise
    except Exception as error:
        report.update(status="error", reason=str(error))
        raise
    finally:
        report["finished_at"] = time()
        runtime._schedule_save()


def compare(members, thresholds, start, end):
    objective = Objective(members, start, end)
    current = fitting.measure(members, objective.current, start, end)
    candidate = fitting.measure(members, thresholds, start, end)
    return {
        "live": {**current["room"], "error_cost": objective.costs(objective.current)[0]},
        "candidate": {**candidate["room"], "error_cost": objective.costs(thresholds)[0]},
    }


async def automatic(runtime, learned, report, source):
    stored = runtime.data["room_learning"][learned["group_id"]]
    previous = stored.get("automatic_apply", {})
    if previous.get("result_id") == learned["id"] and previous.get("status") == "applied":
        report.update(deepcopy(previous))
        return
    report.update(status="skipped", source=source, result_id=learned["id"])
    try:
        guard(runtime, learned, {}, source)
        members, _signature = jobs._context(runtime, learned["group_id"])
        for key, item in members.items():
            item["view"] = runtime._history_view(key)
        end = time()
        report["status"] = "comparing"
        async with runtime._comparison_semaphore:
            patterns = await runtime.hass.async_add_executor_job(
                compare, members, learned["thresholds"], end - 7 * 86400, end
            )
        report["assessment"] = {
            "patterns": {"live": patterns["live"], source: patterns["candidate"]}
        }
        guard(runtime, learned, {}, source)
        _improvement(patterns)
        report["status"] = "applying"
        writes = await apply(runtime, learned["group_id"], learned["id"], source, source)
        report.update(
            writes=writes,
            status="partial" if writes["skipped"] else "applied",
            reason="Compared all zone members on the same fresh recordings; check reported write outcomes.",
        )
    except asyncio.CancelledError:
        report.update(status="interrupted", reason="Joint automatic Apply interrupted")
        raise
    except Exception as error:
        report.update(status="skipped", reason=str(error))
    finally:
        stored["automatic_apply"] = deepcopy(report)
        runtime._schedule_save()


def _improvement(patterns):
    if any(item.get("score") is None for item in patterns.values()):
        raise ValueError("Not enough shared occupied and empty evidence for automatic Apply")
    if not improves(patterns["live"], patterns["candidate"]):
        raise ValueError(
            "Joint recommendation does not reduce the weighted missed-time and false-active penalty"
        )
