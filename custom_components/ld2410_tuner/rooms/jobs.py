"""Server-owned assessment jobs with persisted, versioned results."""

import asyncio
import math
import time
from copy import deepcopy

from ..calibration import settings
from ..calibration import thresholds as threshold_domain
from ..calibration.timing_config import read_timing, timing_signature
from ..const import TRAINING_FIELDS
from ..history import policy
from ..runtime.tasks import finish_task
from ..worker import bridge
from . import assessment, attribution, groups, room_labels, room_sources, sets, truth


def _context(runtime, group_id, sensor=True):
    """``(members, signature)`` for a group. Assessment signatures include room sensor
    activity (``sensor``); learning and Apply signatures leave it out."""
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
        constraints = threshold_domain.read(runtime, entities)
        members[key] = {"thresholds": thresholds, "timing": timing, "constraints": constraints}
        signature[key] = {
            "entities": entities,
            "thresholds": thresholds,
            "timing": timing_signature(timing),
            "revision": device.get("label_revision", 0),
            "training": [device.get(field) for field in TRAINING_FIELDS],
            "constraints": constraints,
        }
    if revisions := truth.revisions(runtime, definitions, group_id, sensor):
        # Absent until a room label exists, so upgraded signatures stay identical.
        signature["room_labels"] = revisions
    return members, signature


def set_context(runtime, group_ids):
    """``({group_id: members}, signature)`` for a learning set.

    One group: exactly ``_context`` without room sensor activity, so one-group results keep
    their signatures. Several: a shared radar is one member entry in every group that has
    it, and the signature holds each group's definition and manual room label revisions
    plus each radar's entry once. A non-default automatic evidence weight is added to
    either (so changing it stales saved results); at the default nothing is added."""
    found, signature = _set_context(runtime, group_ids)
    signature.update(settings.signature(runtime.data))
    signature["evidence_sources"] = _source_signature(runtime, group_ids, found)
    return found, signature


def _source_signature(runtime, group_ids, found):
    definitions = groups.definitions(runtime)[2]
    related = truth.related_to(definitions, group_ids)
    members = {key for group in found.values() for key in group}
    return {
        "retention": policy.settings(runtime.data),
        "members": {
            key: deepcopy(runtime.data["devices"][key].get("presence_sources", {}))
            for key in sorted(members)
        },
        "rooms": {
            key: deepcopy(runtime.data.get("room_presence_sources", {}).get(key, {}))
            for key in related
        },
    }


def _set_context(runtime, group_ids):
    if len(group_ids) == 1:
        members, signature = _context(runtime, group_ids[0], sensor=False)
        return {group_ids[0]: members}, signature
    shared, found = {}, {}
    signature = {"groups": {}, "scorer_version": assessment.SCORER_VERSION}
    for group_id in group_ids:
        members, part = _context(runtime, group_id, sensor=False)
        found[group_id] = {key: shared.setdefault(key, item) for key, item in members.items()}
        signature["groups"][group_id] = part.pop("group")
        if "room_labels" in part:
            signature.setdefault("room_labels", {})[group_id] = part.pop("room_labels")
        part.pop("scorer_version")
        signature.update(part)
    return found, signature


def set_views(runtime, members, since=None):
    """Read each radar's history once; a shared radar's entry is the same in every zone.
    Each view computes its radar's attribution once for the whole job, from the shared
    attribution cache where its inputs are unchanged (``attribution.prepare``)."""
    shared = {}  # The members' guesses, built once for the job's views.
    with attribution.shared_copies(runtime):  # And their labels, copied once.
        for found in members.values():
            for key, item in found.items():
                if "view" not in item:
                    item["view"] = attribution.prepared_view(
                        runtime, key, shared, **({} if since is None else {"since": since})
                    )


async def on_views(runtime, members, job, *args):
    """Run the allowlisted room computations on private process-owned views."""
    from . import application, learning

    operations = {
        assessment.calculate: "room_assess",
        learning._fit_set: "room_fit",
        application._compare_all: "room_compare",
    }
    if job not in operations:
        raise ValueError("Unsupported room worker operation")

    def prepare():
        since = (
            (time.time() - learning.ATTRIBUTION_DAYS * 86400) // 3600 * 3600
            if operations[job] == "room_fit" and learning.ATTRIBUTION_DAYS is not None
            else None
        )
        set_views(runtime, members, since)
        views = {key: item["view"] for found in members.values() for key, item in found.items()}
        metadata = {
            group: {
                key: {
                    **{name: value for name, value in item.items() if name != "view"},
                    "view_key": key,
                }
                for key, item in found.items()
            }
            for group, found in members.items()
        }
        parameters = _worker_parameters(operations[job], metadata, args)
        return {"views": views, "parameters": parameters}

    return await bridge.calculate(runtime, operations[job], prepare)


def _worker_parameters(operation, metadata, args):
    if operation == "room_assess":
        _, start, end, room = args
        return {"members": next(iter(metadata.values())), "start": start, "end": end, "room": room}
    if operation == "room_fit":
        _, start, end, rooms, weight = args
        return {"members": metadata, "start": start, "end": end, "rooms": rooms, "weight": weight}
    _, thresholds, start, end, rooms = args
    return {
        "members": metadata,
        "thresholds": thresholds,
        "start": start,
        "end": end,
        "rooms": rooms,
    }


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


def snapshot_with_inventory(runtime):
    """Return ``(rooms, areas, members, definitions)`` from one registry read."""
    areas, members, definitions = groups.definitions(runtime)
    rooms = {
        "areas": areas,
        "members": members,
        "groups": {
            key: {
                **item,
                "assessment": summary(runtime, key),
                "learning": runtime.data.get("room_learning", {}).get(key),
                "labels": room_labels.room_labels_for(runtime, key),
                "label_revision": runtime.data.get("room_label_revision", {}).get(key, 0),
                "presence_sources": room_sources.summary(runtime, key),
                "estimate": room_labels.cached_estimate(runtime, key, item["device_ids"]),
            }
            for key, item in definitions.items()
        },
    }
    for key in set(runtime._room_estimates) - set(definitions):
        runtime._room_estimates.pop(key)  # Removed groups leave the estimate cache.
    for key in set(runtime._room_recent) - set(runtime.data["devices"]):
        runtime._room_recent.pop(key)  # So do removed radars (their newest sample and runs).
    return rooms, areas, members, definitions


def learning_sets(runtime, definitions):
    """``{set_id: {"group_ids", "device_ids", "learning"}}``.

    ``learning`` is the set's own stored entry (its job, running or finished, and saved
    results), never a zone's entry from before it joined the set. A one-group set's id is
    its group id, so it matches that group's ``learning``."""
    stored = runtime.data.get("room_learning", {})
    return {
        key: {**item, "learning": stored.get(key)}
        for key, item in sets.learning_sets(definitions).items()
    }


def start(runtime, group_id, hours=24):
    if type(hours) not in (int, float) or not math.isfinite(hours) or not 1 <= hours <= 168:
        raise ValueError("Choose between 1 and 168 hours of history")
    if group_id not in runtime._room_jobs:
        members, signature = _context(runtime, group_id)
        room = truth.view(runtime, group_id)
        task = runtime.hass.async_create_task(
            _run(runtime, group_id, members, deepcopy(signature), hours, room)
        )
        runtime._room_jobs[group_id] = task
        task.add_done_callback(lambda done: finish_task(runtime._room_jobs, group_id, done))
    return {"state": "running"}


async def _run(runtime, group_id, members, signature, hours, room=None):
    async with runtime._comparison_semaphore:
        end = time.time()
        try:
            report = await on_views(
                runtime,
                {group_id: members},
                assessment.calculate,
                members,
                end - hours * 3600,
                end,
                room,
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
