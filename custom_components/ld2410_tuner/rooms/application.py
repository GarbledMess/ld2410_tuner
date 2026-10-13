"""Apply a complete joint recommendation through the existing paced radar writer."""

import asyncio
from contextlib import ExitStack
from copy import deepcopy
from time import time

from ..calibration import automatic as policy
from ..calibration import device_io, results
from ..calibration.scoring import improves
from . import fitting, groups, jobs, truth
from .objective import Objective

WORSE = "A zone would get worse"
# Costs are sums of floats; differences below this are integration noise, not change.
NOISE = 1e-9


def _group_ids(learned):
    # Results stored before learning sets were one group, keyed by that group's id.
    return learned.get("group_ids", [learned["group_id"]])


def selected(runtime, set_id, result_id, source):
    set_id = groups.set_id_for(runtime, set_id)
    learned = runtime.data.get("room_learning", {}).get(set_id, {}).get("slots", {}).get(source)
    if not learned or learned["id"] != result_id or learned["method"] != fitting.METHOD:
        raise ValueError("Joint result was replaced or removed; learn the zone again")
    return learned


def guard(runtime, learned, applied, source=None):
    # Resolve through a member zone: the set's id changes when another zone joins it.
    if list(groups.learning_set(runtime, _group_ids(learned)[0])[2]) != _group_ids(learned):
        raise ValueError("The learning set's zones or membership changed; learn again")
    current = jobs.set_context(runtime, _group_ids(learned))[1]
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


async def apply(runtime, set_id, result_id, source="user", automatic_source=None):
    """Apply a learning set's result; ``set_id`` may also be one of its group ids."""
    learned = selected(runtime, set_id, result_id, source)
    stored = runtime.data["room_learning"][learned["group_id"]]
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


def compare(members, thresholds, start, end, room=None):
    """Live and candidate room results for one zone on the same recordings."""
    objective = Objective(members, start, end, room)
    current = fitting.measure(members, objective.current, start, end, room)
    candidate = fitting.measure(members, thresholds, start, end, room)
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
        _zones_permit(learned)
        report["status"] = "comparing"
        zones = await _compare_zones(runtime, learned)
        report["assessment"] = _assessment(zones, source)
        guard(runtime, learned, {}, source)
        _improvement(zones)
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


def _zones_permit(learned):
    """Spec 5: automatic Apply needs every zone scored and none worse after the fit."""
    found = learned.get("groups", {}).values()
    if reason := next((item["skipped"] for item in found if "skipped" in item), None):
        raise ValueError(f"A zone has no usable labels: {reason}")
    if any(item["cost_after"] > item["cost_before"] + NOISE for item in found):
        raise ValueError(WORSE)


async def _compare_zones(runtime, learned):
    """Fresh live-versus-candidate results for every zone of the set, on the same recordings."""
    group_ids = _group_ids(learned)
    members, _signature = jobs.set_context(runtime, group_ids)
    jobs.set_views(runtime, members)
    rooms = {group_id: truth.view(runtime, group_id) for group_id in group_ids}
    end = time()
    async with runtime._comparison_semaphore:
        return await jobs.on_views(
            runtime,
            members,
            _compare_all,
            members,
            learned["thresholds"],
            end - 7 * 86400,
            end,
            rooms,
        )


def _compare_all(members, thresholds, start, end, rooms):
    return {
        group_id: compare(found, thresholds, start, end, rooms[group_id])
        for group_id, found in members.items()
    }


def _assessment(zones, source):
    if len(zones) == 1:
        patterns = next(iter(zones.values()))
        return {"patterns": {"live": patterns["live"], source: patterns["candidate"]}}
    total = _total(zones)
    return {
        "patterns": {"live": total["live"], source: total["candidate"]},
        "zones": {
            group_id: {"live": item["live"], source: item["candidate"]}
            for group_id, item in zones.items()
        },
    }


def _total(zones):
    """Summed penalties of a linked set; no single room score spans its zones."""
    return {
        side: {"error_cost": sum(item[side]["error_cost"] for item in zones.values())}
        for side in ("live", "candidate")
    }


def _improvement(zones):
    if any(item.get("score") is None for zone in zones.values() for item in zone.values()):
        raise ValueError("Not enough shared occupied and empty evidence for automatic Apply")
    if len(zones) > 1 and any(
        item["candidate"]["error_cost"] > item["live"]["error_cost"] + NOISE
        for item in zones.values()
    ):
        raise ValueError(WORSE)
    patterns = next(iter(zones.values())) if len(zones) == 1 else _total(zones)
    if not improves(patterns["live"], patterns["candidate"]):
        raise ValueError(
            "Joint recommendation does not reduce the weighted missed-time and false-active penalty"
        )
