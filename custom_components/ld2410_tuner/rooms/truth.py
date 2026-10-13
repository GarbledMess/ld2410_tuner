"""Room truth at time t: own labels, implied labels, member labels, then automatic evidence."""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Callable, Iterable
from copy import deepcopy
from itertools import chain
from typing import Any, TypedDict

from ..calibration.constants import MIN_AUTO_CONFIDENCE
from ..history.labels import _label_events, _label_index
from . import evidence, groups

MANUAL = frozenset({"manual"})
SENSOR = frozenset({"sensor"})

Label = tuple[str, str, float]
Reader = Callable[[float], Label | None]


class TruthContext(TypedDict):
    own: Reader
    parent: Reader | None
    zones: list[Reader]
    members: dict[str, Any]
    sensor: Reader
    estimate: Reader | None
    boundaries: set[float]


def reader(labels, sources) -> Reader:
    """Read ``(state, origin, confidence)`` from labels of ``sources``; the last label wins.
    An automatic (sensor) label below ``MIN_AUTO_CONFIDENCE`` is no evidence, as for device
    sources and recorded guesses."""
    intervals = [
        {**item, "state": value}
        for item in labels or ()
        if item.get("source") in sources and (value := _value(item))[2] >= MIN_AUTO_CONFIDENCE
    ]
    times, states = _label_index(_label_events(intervals))

    def read(timestamp):
        index = bisect_right(times, timestamp) - 1
        return states[index] if index >= 0 else None

    read.edges = times  # Where the answer can change, in order.
    return read


def _value(item):
    if item.get("source") == "manual":
        return item.get("state", "unknown"), "human", 1.0
    return item.get("state", "unknown"), "automatic", float(item.get("confidence", 1.0))


def related(definitions, group_id):
    """Return ``(group, area parent, zones)``; nothing flows across area-less groups."""
    group = definitions.get(group_id) or {}
    area_id = group.get("area_id")
    if area_id is None:
        return group_id, None, []
    if group.get("kind") == "area":
        zones = [
            key
            for key, item in definitions.items()
            if item.get("kind") == "zone" and item.get("area_id") == area_id
        ]
        return group_id, None, zones
    return group_id, f"area:{area_id}", []


def revisions(runtime, definitions, group_id, sensor=True) -> dict[str, int]:
    """Non-zero room label revisions that can change this group's truth. ``sensor`` False
    leaves out room sensor activity: it stales assessments (spec 6) but, like device-level
    sources, never learning results or Apply."""
    manual = runtime.data.get("room_label_revision", {})
    sensors = runtime.data.get("room_sensor_revision", {}) if sensor else {}
    totals = {
        key: manual.get(key, 0) + sensors.get(key, 0) for key in related_ids(definitions, group_id)
    }
    return {key: total for key, total in totals.items() if total}


def view(runtime, group_id) -> dict[str, Any]:
    """Snapshot the definitions and relevant room labels on the event loop for a job."""
    _, _, definitions = groups.definitions(runtime)
    saved = runtime.data.get("room_labels", {})
    labels = {
        key: deepcopy(saved[key]) for key in related_ids(definitions, group_id) if key in saved
    }
    return {"group_id": group_id, "groups": deepcopy(definitions), "room_labels": labels}


def related_ids(definitions, group_id):
    """Groups whose labels can change this group's truth: itself, its area, its zones."""
    own, parent, zones = related(definitions, group_id)
    return [key for key in (own, parent, *zones) if key]


def related_to(definitions, group_ids) -> list[str]:
    """``related_ids`` of every one of ``group_ids``, sorted and without repeats."""
    return sorted({key for group_id in group_ids for key in related_ids(definitions, group_id)})


def context(runtime_view, group_id, inputs, estimate=None) -> TruthContext:
    """Build the readers for one group; ``runtime_view`` None means no room labels."""
    runtime_view = runtime_view or {}
    labels = runtime_view.get("room_labels", {})
    own, parent, zones = related(runtime_view.get("groups", {}), group_id)
    used = [labels.get(key, []) for key in (own, parent, *zones) if key]
    return {
        "own": reader(labels.get(own), MANUAL),
        "parent": reader(labels.get(parent), MANUAL) if parent else None,
        "zones": [reader(labels.get(key), MANUAL) for key in zones],
        "members": inputs,
        "sensor": reader(labels.get(own), SENSOR),
        "estimate": estimate,
        "boundaries": {item[edge] for items in used for item in items for edge in ("start", "end")},
    }


def edges(context: TruthContext, estimate=True) -> Iterable[float]:
    """Every time at which the truth of ``context`` can change its answer (some more than
    once), for readers kept over many samples (``attribution.Memo``): its room labels' edges,
    the edges of the labels of its members (``edges`` of each) and, unless ``estimate`` is
    False, of the guesses of its estimate. Without them it still says where the answers that
    are human can change: those never read the estimate."""
    return chain(
        context["boundaries"],
        *(item.get("edges", ()) for item in context["members"].values()),
        getattr(context["estimate"], "edges", ()) if estimate else (),
    )


def for_view(runtime_view, inputs) -> TruthContext:
    """Truth for a snapshot; the estimate is automatic evidence and never reaches human readers."""
    return context(
        runtime_view, (runtime_view or {}).get("group_id"), inputs, estimate_reader(inputs)
    )


def estimate(guesses) -> Label | None:
    """Room estimate (spec 4.4) from member ``(state, confidence)`` guesses, ``None`` if unsure.
    Present needs ``AGREEING_MEMBERS`` members saying so (all of a smaller group): a radar's
    own guess must not be a room's."""
    present = [conf for state, conf in filter(None, guesses) if state == "present"]
    if present:
        if not evidence.agreed(len(present), len(guesses)):
            return None
        miss = 1.0
        for conf in present:
            miss *= 1.0 - conf
        return _floor(("present", "automatic", 1.0 - miss))
    if guesses and all(item and item[0] == "not_present" for item in guesses):
        return _floor(("not_present", "automatic", min(item[1] for item in guesses)))
    return None


def _floor(label):
    return label if label[2] >= MIN_AUTO_CONFIDENCE else None


def estimate_reader(inputs) -> Reader:
    """Read the estimate at ``t`` from every member's recorded automatic guess."""

    def read(timestamp):
        guesses = []
        for item in inputs.values():
            guess = item["automatic"].at(timestamp)
            guesses.append((guess[0], guess[2]) if guess else None)
        return estimate(guesses)

    def bulk(times):
        """``read`` at each of the ``times``, decided once for each combination of guesses."""
        columns = [list(map(item["automatic"].at, times)) for item in inputs.values()]
        decided = {}
        if not columns:
            return [estimate([])] * len(times)
        for combination in zip(*columns, strict=True):
            if combination not in decided:
                decided[combination] = estimate([(g[0], g[2]) if g else None for g in combination])
        return [decided[combination] for combination in zip(*columns, strict=True)]

    read.bulk = bulk
    return read


def room_truth(context: TruthContext, timestamp: float) -> Label | None:
    """First match wins (spec 4.2); an explicit human Unknown at levels 1-3 stops fallback.

    Level 3 derives from member radars' labels; where they carry no human answer, the
    room's own sensor labels come before what members' recorded guesses derive, then the
    estimate. Contexts whose members carry no guesses (attribution) read the same order."""
    own = context["own"](timestamp)
    if own is not None:
        return own if own[0] in evidence.LABELS else None
    implied = _implied(context, timestamp)
    if implied:
        return implied
    members = context["members"].values()
    manuals = [item["manual"](timestamp) for item in members]
    derived = evidence.combine(
        [
            evidence.member_label(label, item, timestamp)
            for label, item in zip(manuals, members, strict=True)
        ]
    )
    # Human iff every member answered from its own labels (or one labelled Present); an
    # explicit human Unknown of a member stops the fallback.
    if (derived and derived[1] == "human") or any(
        label is not None and label not in evidence.LABELS for label in manuals
    ):
        return derived
    return _sensor(context, timestamp) or derived or _estimate(context, timestamp)


ESTIMATE = object()  # An answer that waits for the estimate.


def room_truths(context: TruthContext, times) -> list:
    """``room_truth`` at each of the ``times``, read level by level for all of them and decided
    once for each combination of what the levels read."""
    members = list(context["members"].values())
    columns = [
        list(map(context["own"], times)),
        list(map(context["parent"], times)) if context["parent"] else [None] * len(times),
        *(list(map(zone, times)) for zone in context["zones"]),
        *(list(map(item["manual"], times)) for item in members),
        *(list(map(item["automatic"].at, times)) for item in members),
        list(map(context["sensor"], times)),
    ]
    zones, estimate = len(context["zones"]), context["estimate"]
    decided, answers = {}, []
    for key in zip(*columns, strict=True):
        if key not in decided:
            decided[key] = _decide(key, zones, len(members), bool(estimate))
        answers.append(decided[key])
    waiting = [index for index, answer in enumerate(answers) if answer is ESTIMATE]
    if waiting:
        wanted = [times[index] for index in waiting]
        bulk = getattr(estimate, "bulk", None)
        for index, answer in zip(
            waiting, bulk(wanted) if bulk else map(estimate, wanted), strict=True
        ):
            answers[index] = answer
    return answers


def _decide(key, zones, members, estimated):
    """``room_truth`` from what its levels read (see ``room_truths``); ``ESTIMATE`` where the
    estimate decides."""
    own, parent, *rest = key
    if own is not None:
        return own if own[0] in evidence.LABELS else None
    if parent and parent[0] == "not_present":
        return "not_present", "human", 1.0
    if any((zone or ("",))[0] == "present" for zone in rest[:zones]):
        return "present", "human", 1.0
    manuals, autos = rest[zones : zones + members], rest[zones + members : zones + 2 * members]
    derived = evidence.combine(
        [
            (manual, "human", 1.0)
            if manual in evidence.LABELS
            else None
            if manual is not None
            else auto
            for manual, auto in zip(manuals, autos, strict=True)
        ]
    )
    if (derived and derived[1] == "human") or any(
        label is not None and label not in evidence.LABELS for label in manuals
    ):
        return derived
    sensor = rest[-1]
    sensor = sensor if sensor is not None and sensor[0] in evidence.LABELS else None
    return sensor or derived or (ESTIMATE if estimated else None)


def _implied(context, timestamp):
    parent = context["parent"]
    if parent and (parent(timestamp) or ("",))[0] == "not_present":
        return "not_present", "human", 1.0
    if any((zone(timestamp) or ("",))[0] == "present" for zone in context["zones"]):
        return "present", "human", 1.0
    return None


def _sensor(context, timestamp):
    sensor = context["sensor"](timestamp)
    return sensor if sensor is not None and sensor[0] in evidence.LABELS else None


def _estimate(context, timestamp):
    estimate = context["estimate"]
    return estimate(timestamp) if estimate else None


def _automatic(context, timestamp):
    """Level 4 alone: the room's sensor labels, then the estimate."""
    return _sensor(context, timestamp) or _estimate(context, timestamp)
