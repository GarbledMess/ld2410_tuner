"""Time-weighted room assessment; no learning, threshold writes or live presence entity."""

from itertools import chain

from ..calibration.constants import MIN_CLASS_SAMPLES, MIN_RECALL, MISSED_TIME_COST
from ..calibration.scoring import error_cost, score_from_cost
from . import evidence, replay, roles, truth
from .intervals import seconds

SCORER_VERSION = 1


def calculate(members, start, end, room=None):
    """Assess ``members``; ``room`` is a ``truth.view`` snapshot (None: no room labels)."""
    inputs = {
        key: evidence.read(item["view"], key, item["thresholds"], start, end)
        for key, item in members.items()
    }
    context = truth.for_view(room, inputs)
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
    _samples(context, timelines, totals)
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
        "sample_basis": "Unique observation timestamps within shared recording coverage",
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


def _samples(context, timelines, totals):
    times = sorted(
        set(chain.from_iterable(item["rows"].times for item in context["members"].values()))
    )
    covered = [ts for ts in times if timelines["room"]["coverage"].at(ts)]
    for ts, label in zip(covered, truth.room_truths(context, covered), strict=True):
        if label is None:
            continue
        state, source, _confidence = label
        for key, timeline in timelines.items():
            total = totals[key][source][state]
            total["samples"] += 1
            total["hits"] += bool(timeline["upper" if state == "not_present" else "lower"].at(ts))


def _result(totals):
    sources = {label: _source(totals, label) for label in evidence.LABELS}
    result = {"outcomes": totals, "sources": sources, "score": None}
    if None in sources.values():
        return {
            **result,
            "reason": "Need shared occupied and empty recordings, with at least 50 labelled observation times for each state.",
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
