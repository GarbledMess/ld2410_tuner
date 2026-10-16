"""Time-weighted room assessment; no learning, threshold writes or live presence entity."""

from ..calibration.constants import MIN_CLASS_SAMPLES, MIN_RECALL, MISSED_TIME_COST, SAMPLE_SECONDS
from ..calibration.scoring import error_cost, score_from_cost
from . import adaptive, evidence, replay, roles, truth
from .intervals import seconds

SCORER_VERSION = 2


def calculate(members, start, end, room=None, *, quality=None):
    """Assess ``members``; ``room`` is a ``truth.view`` snapshot (None: no room labels)."""
    quality = None if quality is None else adaptive.models(quality)
    inputs = {
        key: evidence.read(
            item["view"],
            key,
            item["thresholds"],
            start,
            end,
            automatic_reader=adaptive.CorrectedReader(quality[key]) if quality else None,
        )
        for key, item in members.items()
    }
    context = (
        truth.for_view(room, inputs) if quality is None else adaptive.corrected_truth(room, inputs)
    )
    radars = {
        key: replay.radar(
            inputs[key]["rows"], item["thresholds"], item["timing"], inputs[key]["barriers"]
        )
        for key, item in members.items()
    }
    combined = replay.combined(radars)
    timelines = {key: replay.lookups(item) for key, item in radars.items()}
    timelines["room"] = replay.lookups(combined)
    boundaries = _boundaries(inputs, radars, start, end, context["boundaries"])
    totals = {key: _empty() for key in timelines}
    excluded = {
        "no_shared_recording_seconds": max(0.0, end - start - seconds(combined["coverage"])),
        "unlabelled_seconds": 0.0,
    }
    contributions = dict.fromkeys(members, 0.0)
    seen = roles.attributed_yes(members, room)
    tallies = {key: roles.empty_roles() for key in members}
    for a, b in zip(boundaries, boundaries[1:], strict=False):
        label = _measure_span(a, b, context, timelines, totals, excluded, contributions)
        if label:
            _tally(tallies, seen, timelines, label, (a + b) / 2, b - a)
    _duration_counts(totals)
    return {
        "scorer_version": SCORER_VERSION,
        "start": start,
        "end": end,
        "room": _result(totals["room"]),
        "devices": {
            key: {
                **_result(totals[key]),
                "exclusive_presence_seconds": contributions[key],
                **tallies[key],
            }
            for key in members
        },
        "excluded": excluded,
        "shared_recording_seconds": seconds(combined["coverage"]),
        "timing": {key: item["timing"] for key, item in members.items()},
        "sample_basis": "Observed recording duration; unchanged values count for how long they hold",
        "label_basis": "Any member labelled present; empty requires every member labelled empty. Human labels override estimates on the same member.",
    }


def _boundaries(inputs, radars, start, end, labelled):
    times = {start, end, *labelled}
    for item in inputs.values():
        times.update(item["boundaries"])
    for item in radars.values():
        times.update(point for spans in item.values() for span in spans for point in span)
    return sorted(t for t in times if start <= t <= end)


def _empty():
    return {
        source: {
            label: {
                "seconds": 0.0,
                "active_seconds": 0.0,
                "possible_seconds": 0.0,
                "weighted_seconds": 0.0,
                "weighted_active_seconds": 0.0,
                "samples": 0,
                "hits": 0,
            }
            for label in evidence.LABELS
        }
        for source in ("human", "automatic")
    }


def _measure_span(start, end, context, timelines, totals, excluded, contributions):
    midpoint = (start + end) / 2
    if not timelines["room"]["coverage"].at(midpoint):
        return None
    label = truth.room_truth(context, midpoint)
    if label is None:
        excluded["unlabelled_seconds"] += end - start
        return None
    state, source, confidence = label
    detected = []
    for key, timeline in timelines.items():
        hit = timeline["upper" if state == "not_present" else "lower"].at(midpoint, False)
        possible = timeline["possible"].at(midpoint, False)
        _add(totals[key][source][state], end - start, hit, possible, confidence)
        if key != "room" and hit:
            detected.append(key)
    if state == "present" and len(detected) == 1:
        contributions[detected[0]] += end - start
    return label


def _tally(tallies, seen, timelines, label, midpoint, duration):
    """Per-member role seconds for one span. Spans are not split at attribution episode
    edges (sample times), so a span straddling one is classified wholly by its midpoint:
    an error of at most one sample cadence per episode edge."""
    state = label[0]
    side = "upper" if state == "not_present" else "lower"
    for key, tally in tallies.items():
        hit = timelines[key][side].at(midpoint, False)
        roles.add(tally, duration, state, hit, *roles.attributed(seen[key], label, midpoint))


def _add(total, duration, hit, possible, confidence):
    total["seconds"] += duration
    total["active_seconds"] += duration * hit
    total["possible_seconds"] += duration * possible
    total["weighted_seconds"] += duration * confidence
    total["weighted_active_seconds"] += duration * confidence * hit


def _duration_counts(totals):
    """Native admission uses observed duration in the legacy six-second support unit."""
    for radar in totals.values():
        for source in radar.values():
            for total in source.values():
                total["samples"] = total["seconds"] / SAMPLE_SECONDS
                total["hits"] = total["active_seconds"] / SAMPLE_SECONDS


def _result(totals):
    sources = {label: _source(totals, label) for label in evidence.LABELS}
    result = {"outcomes": totals, "sources": sources, "score": None}
    if None in sources.values():
        return {
            **result,
            "reason": "Need enough shared occupied and empty recordings to meet the minimum evidence requirement.",
        }
    present = totals[sources["present"]]["present"]
    empty = totals[sources["not_present"]]["not_present"]
    miss = 1 - present["weighted_active_seconds"] / present["weighted_seconds"]
    false = empty["weighted_active_seconds"] / empty["weighted_seconds"]
    return {
        **result,
        "score": score_from_cost(error_cost(miss, false)),
        "presence_recall": 1 - miss,
        "false_positive_percent": 100 * false,
        "target_met": 1 - miss >= MIN_RECALL - 1e-12,
        "basis": "human" if set(sources.values()) == {"human"} else "estimated",
        "missed_time_cost": MISSED_TIME_COST,
    }


def _source(totals, label):
    if sum(source[label]["samples"] for source in totals.values()) < MIN_CLASS_SAMPLES:
        return None
    return next(
        (
            source
            for source in ("human", "automatic")
            if totals[source][label]["weighted_seconds"] > 0
        ),
        None,
    )
