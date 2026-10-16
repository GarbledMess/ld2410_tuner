"""Device learning on complete physical traces before intersecting target labels."""

from bisect import bisect_left, bisect_right
from copy import copy
from dataclasses import replace
from math import ceil, inf

from ..history import guesses, spans, summaries
from ..history.label_lookup import _history_label_reader
from ..rooms import attribution, evidence, fitting
from ..rooms.intervals import intersect, seconds, union
from ..rooms.objective import Objective, Radar
from . import evidence_plan, summary_quality
from .adaptive import earlier_view
from .constants import (
    MAX_CLASS_SAMPLES,
    METHOD,
    MIN_AUTO_CONFIDENCE,
    MIN_CLASS_SAMPLES,
    MIN_RECALL,
    MISSED_TIME_COST,
    SAMPLE_SECONDS,
)
from .diagnostics import review_evidence
from .episodes import chronological_split
from .event_preferences import preferences, prepare
from .feasibility import exclusive_presence
from .intervals import WeightedTime
from .metrics import metrics
from .quality import QUALITY_VERSION, QualityModel, learn
from .quality_evidence import collect
from .scoring import error_cost
from .selection import select
from .settings import DEFAULT_AUTOMATIC_WEIGHT


def _edges(reader):
    if reader is None:
        return set()
    return set(getattr(reader, "edges", ())).union(
        *(_edges(part) for part in getattr(reader, "parts", ()))
    )


class _Targets:
    def __init__(self, view, device_id, model, start, end):
        self.model = model
        self.own = _history_label_reader(view.data["devices"][device_id])
        self.raw = model.context.raw_reader()
        self.room = _room_evidence(view, device_id, self.raw, start, end)
        self.edges = sorted(
            _edges(self.own)
            | _edges(self.room)
            | {point for run in model.context.raw_runs for point in run[:2]}
        )

    def manual(self, timestamp):
        own = self.own(timestamp)
        if own is not None:
            return own
        room = self.room(timestamp) if self.room else None
        return room[0] if attribution.is_human(room) else None

    def at(self, timestamp, default=None):
        if self.manual(timestamp) is not None:
            return default
        room = self.room(timestamp) if self.room else None
        label = room or self.raw.at(timestamp)
        if label is None or not MIN_AUTO_CONFIDENCE <= label[2] <= 1:
            return default
        basis = guesses.UNKNOWN if room else self.model.context.basis_at(timestamp)
        return label[0], "automatic", self.model.correct(label[2], basis, label[0])

    def cells(self, start, end):
        cuts = sorted({start, end, *(edge for edge in self.edges if start < edge < end)})
        return [
            (a, b, value)
            for a, b in zip(cuts, cuts[1:], strict=False)
            if (value := self.at(a)) is not None
        ]


def _cell_visible(rows, index, low):
    return index >= 0 and (rows.ends is None or rows.ends[index] is None or low < rows.ends[index])


def _regions(inputs, start, end):
    rows = inputs["rows"]
    cuts = sorted(
        {
            start,
            end,
            *inputs["boundaries"],
            *rows.times,
            *(value for value in rows.ends or () if value is not None),
        }
    )
    result = []
    for low, high in zip(cuts, cuts[1:], strict=False):
        index = bisect_right(rows.times, low) - 1
        if not _cell_visible(rows, index, low) or low < start or high > end:
            continue
        label = evidence.device_label(inputs, low)
        if label is not None and label[0] in evidence.LABELS:
            result.append((low, high, rows[index][1], *label))
    return result


class _DeviceObjective(Objective):
    def __init__(
        self,
        view,
        device_id,
        keys,
        current,
        timing,
        weight,
        constraints,
        start,
        end,
        model,
        reference=False,
    ):
        self.device_id = device_id
        self._preference_histograms = None
        targets = _Targets(view, device_id, model, start, end)
        self.plan = _learning_plan(view, device_id, keys, start, end, timing, targets)
        self._mass_scaled = False
        physical_view = _planned_view(view, device_id, self.plan)
        inputs = evidence.read(physical_view, device_id, keys, -inf, end, automatic_reader=targets)
        inputs["manual"] = targets.manual
        inputs["boundaries"] = {
            start,
            end,
            *(edge for edge in {*inputs["boundaries"], *targets.edges} if start <= edge <= end),
        }
        if self.plan is not None:
            inputs["boundaries"].update(point for core in self.plan.cores for point in core[:2])
        self.regions = _regions(inputs, start, end)
        if self.plan is not None:
            self.regions = [region for region in self.regions if self.plan.scale_at(region[0]) > 0]
        self.physical_inputs = inputs
        item = {
            "view": view,
            "thresholds": current,
            "timing": timing,
            "constraints": constraints or {},
        }
        item.update(adaptive_inputs=inputs, adaptive_radar=Radar(item, inputs, model))
        super().__init__(
            {device_id: item},
            start,
            end,
            automatic_weight=weight,
            quality={device_id: model},
            reference=reference,
        )

    def _sources(self, automatic_weight):
        if self.plan is not None and not self._mass_scaled:
            self.mass = {
                key: WeightedTime(
                    mass.times,
                    [
                        value * self.plan.scale_at(timestamp)
                        for timestamp, value in zip(mass.times, mass.weights, strict=True)
                    ],
                )
                for key, mass in self.mass.items()
            }
            self.totals = {
                key: mass.between(self.start, self.end) for key, mass in self.mass.items()
            }
            self._mass_scaled = True
        super()._sources(automatic_weight)

    def _support(self, label):
        if self.plan is not None:
            model = self.quality[self.device_id]
            return model.human_episodes(label), model.episode_mass(label)
        return super()._support(label)

    def _member_preferences(self, members, room):
        if self._preference_histograms is None:
            self._preference_histograms = prepare(
                self.regions,
                self.current[self.device_id],
                quality=self.quality[self.device_id],
                scale=None if getattr(self, "plan", None) is None else self.plan.scale_at,
            )
        suppressed = {
            state for state in evidence.LABELS if self.reference and self.totals["human", state]
        }
        return {
            self.device_id: preferences(
                self.regions,
                self.current[self.device_id],
                quality=self.quality[self.device_id],
                reference=suppressed,
                prepared=self._preference_histograms,
            )
        }


def _intervals(objective, thresholds):
    radar = objective.radars[objective.device_id]
    projected = radar.project(radar.bits(thresholds))
    return {kind: list(zip(*value, strict=True)) for kind, value in projected.items()}


def _source_totals(objective, thresholds, source, start, end, effective):
    projected = _intervals(objective, thresholds)
    target = _targets(objective, source, start, end, effective)
    hits = {
        state: intersect(target[state], projected["lower" if state == "present" else "upper"])
        for state in target
    }
    return target, hits


def _targets(objective, source, start, end, effective):
    """Each state's ``source`` regions within ``[start, end)``, covered by the recording: the
    regions are joined first, so coverage is intersected once per state, not per region.
    Thresholds never change them, so they are kept on the objective."""
    suppressed = tuple(
        state
        for state in evidence.LABELS
        if effective
        and source == "automatic"
        and objective.reference
        and objective.totals["human", state]
    )
    key = source, start, end, suppressed
    kept = objective.__dict__.setdefault("_measured_targets", {})
    if key not in kept:
        coverage = objective.radars[objective.device_id].projector.coverage
        regions = {state: [] for state in evidence.LABELS}
        for low, high, _values, state, origin, _confidence in objective.regions:
            if origin == source and state not in suppressed:
                regions[state].append((max(start, low), min(end, high)))
        kept[key] = {
            state: union(intersect(coverage, union(items))) for state, items in regions.items()
        }
    return kept[key]


def _measure(objective, thresholds, source="human", *, start=None, end=None, effective=True):
    start = objective.start if start is None else start
    end = objective.end if end is None else end
    target, hits = _source_totals(objective, thresholds, source, start, end, effective)
    present, empty = (_seconds(objective, target[state]) for state in evidence.LABELS)
    detected, false = (_seconds(objective, hits[state]) for state in evidence.LABELS)
    recall = detected / present if present else None
    episodes = _parent_coverage(objective, target["present"], source)
    active = _Within(hits["present"])
    missed = sum(not any(active(a, b) for a, b in pieces) for pieces in episodes)
    longest = _longest_inactive(target["present"], hits["present"])
    duration = {
        "present_seconds": present,
        "empty_seconds": empty,
        "missed_seconds": present - detected,
        "missed_seconds_upper": present - detected,
        "false_positive_seconds": false,
        "false_positive_percent": 100 * false / empty if empty else None,
        "false_positive_score": -100 * false / empty if empty else None,
        "presence_recall": recall,
        "presence_recall_lower": recall,
        "presence_episodes": len(episodes),
        "missed_presence_episodes": missed,
        "false_trigger_events": len(hits["not_present"]),
        "unscored_presence_samples": 0,
        "unscored_empty_samples": 0,
        "basis": _duration_basis(objective),
        "missed_time_cost": MISSED_TIME_COST,
        "longest_missed_seconds": longest,
        "longest_missed_seconds_upper": longest,
        "error_cost": error_cost(
            1 - recall if recall is not None else 0, false / empty if empty else 0
        ),
    }
    return {
        "duration": duration,
        "presence_episodes": len(episodes),
        "missed_presence_episodes": missed,
        "longest_missed_run_samples": duration["longest_missed_seconds"] / SAMPLE_SECONDS,
        "false_trigger_bursts": len(hits["not_present"]),
        "false_trigger_bursts_per_hour": len(hits["not_present"]) * 3600 / empty if empty else 0,
        "observed_absent_seconds": empty,
        "present_samples": present / SAMPLE_SECONDS,
        "not_present_samples": empty / SAMPLE_SECONDS,
        "sensitivity": recall or 0,
        "false_positive_rate": false / empty if empty else 0,
        "false_negatives": (present - detected) / SAMPLE_SECONDS,
        "false_positives": false / SAMPLE_SECONDS,
    }


def _duration_basis(objective):
    return (
        "physical_interval_time"
        if getattr(objective, "plan", None) is None
        else "representative_physical_interval_time"
    )


def _seconds(objective, intervals):
    return (
        seconds(intervals)
        if getattr(objective, "plan", None) is None
        else objective.plan.seconds(intervals)
    )


def _parent_coverage(objective, targets, source):
    context = objective.quality[objective.device_id].context
    parents = (
        context.episodes
        if source == "human"
        else tuple({item.episode for item in context.automatic_episodes})
    )
    present = [ep for ep in parents if ep.state == "present"]
    if not present:
        return [[item] for item in targets]
    within = _Within(targets)
    return [pieces for ep in present if (pieces := within(ep.start, ep.end))]


def _longest_inactive(target, hits):
    longest = 0.0
    within = _Within(hits)
    for start, end in target:
        boundaries = [start, *(edge for interval in within(start, end) for edge in interval), end]
        longest = max(
            longest, *(b - a for a, b in zip(boundaries[::2], boundaries[1::2], strict=True))
        )
    return longest


class _Within:
    """``intersect(intervals, [(low, high)])`` for sorted, disjoint ``intervals``, found by
    bisection instead of walking them from the start for every query."""

    def __init__(self, intervals):
        self.intervals = intervals
        self.starts = [low for low, _high in intervals]
        self.ends = [high for _low, high in intervals]

    def __call__(self, low, high):
        first, stop = bisect_right(self.ends, low), bisect_left(self.starts, high)
        return [
            (max(a, low), min(b, high))
            for a, b in self.intervals[first:stop]
            if min(b, high) > max(a, low)
        ]


SUMMARY_ROWS = 50_000
LEARNING_ROWS = 50_000


def _dense(view, device_id, start, end):
    rows = 0
    for day, selected in spans._selected_days(view, device_id, start, end):
        rows += sum(
            bisect_left(day.times, high) - bisect_right(day.ends, low) for low, high in selected
        )
        if rows > SUMMARY_ROWS:
            return True
    return False


def _learning_plan(view, device_id, keys, start, end, timing, targets):
    def read(timestamp):
        manual = targets.manual(timestamp)
        if manual in evidence.LABELS:
            return manual, "human"
        if manual is not None:
            return None
        value = targets.at(timestamp)
        return None if value is None else value[:2]

    read.edges = targets.edges
    return evidence_plan.plan(
        view, device_id, keys, start, end, timing or {}, read, limit=LEARNING_ROWS
    )


def _planned_view(view, device_id, plan):
    if plan is None:
        return view
    selected = copy(view)
    selected._learning_windows = {
        **getattr(view, "_learning_windows", {}),
        device_id: plan.contexts,
    }
    return selected


def _room_evidence(view, device_id, raw, start, end):
    own = _history_label_reader(view.data["devices"][device_id])
    if _dense(view, device_id, start, end) and summaries.fully_labelled(
        view, device_id, own, start, end
    ):
        return None
    return attribution.select(
        attribution.room_targets(view, device_id),
        attribution.auto_targets(view, device_id, reader=raw),
    )


def _quality_context(view, device_id, keys, start, end, human):
    collector = summary_quality.collect if _dense(view, device_id, start, end) else collect
    return collector(view, device_id, keys, start=start, end=end, human_reader=human, owner=None)


def _objective(
    view,
    device_id,
    keys,
    current,
    timing,
    weight,
    constraints,
    start,
    end,
    reference=False,
    quality_context=None,
):
    raw = guesses.of(view, device_id)
    room = _room_evidence(view, device_id, raw, start, end)

    def human(timestamp):
        answer = room(timestamp) if room else None
        return answer[0] if attribution.is_human(answer) else None

    human.edges = sorted(_edges(room))
    context = quality_context or _quality_context(view, device_id, keys, start, end, human)
    model = context if isinstance(context, QualityModel) else learn(context)
    if quality_context is False:
        bases = {guesses.UNKNOWN, *(run[4] for run in model.context.raw_runs)}
        model = replace(
            model,
            posteriors={(basis, state): 1.0 for basis in bases for state in evidence.LABELS},
            regimes={},
        )
    return _DeviceObjective(
        view, device_id, keys, current, timing, weight, constraints, start, end, model, reference
    )


def assess(view, device_id, thresholds, timing=None, *, quality_context=None, start=-inf, end=inf):
    """One fixed pattern ``thresholds`` measured on the radar's labelled history (never
    refitted); quality-corrected unless ``quality_context`` is False."""
    start, end = _bounds(view, device_id, start, end)
    objective = _objective(
        view,
        device_id,
        list(thresholds),
        dict(thresholds),
        timing or {},
        1,
        None,
        start,
        end,
        quality_context=quality_context,
    )
    return _assess_objective(objective, thresholds, quality_context is not False)


def _assess_objective(objective, thresholds, corrected):
    thresholds = {key: int(value) for key, value in thresholds.items()}
    rates = objective.rates({objective.device_id: thresholds})["automatic"]
    return {
        "human": _measure(objective, thresholds),
        "automatic": _measure(objective, thresholds, "automatic", effective=False),
        "automatic_rank": (error_cost(*rates), *rates),
        "quality": objective.quality[objective.device_id].audit() if corrected else None,
        "automatic_evidence": _automatic_evidence(
            objective, _measure(objective, thresholds, "automatic", effective=False), 1
        ),
    }


def compare(view, device_id, context):
    from .comparison import SCORE_MODEL, best_slots

    start, end = _bounds(view, device_id, -inf, inf)
    objective = _objective(
        view,
        device_id,
        context["keys"],
        dict.fromkeys(context["keys"], 100),
        context["timing"],
        1,
        None,
        start,
        end,
        quality_context=None if context.get("quality_version") else False,
    )
    corrected = bool(context.get("quality_version"))
    patterns = {
        name: _compare_pattern(pattern, objective, context["keys"], corrected)
        for name, pattern in context["patterns"].items()
    }
    counts = {
        source: {
            state: sum(
                (b - a) / SAMPLE_SECONDS
                for a, b, _, label, origin, _ in objective.regions
                if label == state and origin == source
            )
            for state in evidence.LABELS
        }
        for source in ("human", "automatic")
    }
    attribution.release([view])
    result = {
        "model": SCORE_MODEL,
        "patterns": patterns,
        "best_slots": best_slots(patterns),
        "counts": counts,
        "timing": context["timing"],
        "incomplete_samples": sum(
            (b - a) / SAMPLE_SECONDS
            for a, b, readings, *_ in objective.regions
            if any(key not in readings for key in context["keys"])
        ),
        "outliers": {"method": "native_intervals_retained", "excluded": False},
        "missed_time_cost": MISSED_TIME_COST,
    }
    if corrected:
        result["quality"] = objective.quality[device_id].audit()
    return result


def _compare_pattern(pattern, objective, keys, corrected):
    from .comparison import _source, complete_thresholds
    from .scoring import score_from_cost

    if pattern is None:
        return {"score": None, "reason": "No saved result", "applicable": False}
    values = pattern["thresholds"]
    valid = complete_thresholds(values, keys) and all(
        int(value) == value for value in values.values()
    )
    result = {
        "result_id": pattern.get("id"),
        "applicable": pattern["applicable"] and valid,
        "thresholds_complete": valid,
        "score": None,
    }
    if not valid:
        return {
            **result,
            "reason": "A complete integer threshold set for the active gates is unavailable",
        }
    measured = _assess_objective(objective, values, corrected)
    human, automatic = measured["human"], measured["automatic"]
    sources = {state: _source(state, human, automatic) for state in evidence.LABELS}
    result.update(human=human, automatic=automatic, sources=sources)
    if None in sources.values():
        return {**result, "reason": "Need 300 observed seconds of each state"}
    _, auto_miss, auto_false = measured["automatic_rank"]
    miss = (
        1 - human["duration"]["presence_recall_lower"]
        if sources["present"] == "human"
        else auto_miss
    )
    false = (
        human["duration"]["false_positive_percent"] / 100
        if sources["not_present"] == "human"
        else auto_false
    )
    cost = error_cost(miss, false)
    duration = human["duration"]
    return {
        **result,
        "score": score_from_cost(cost),
        "error_cost": cost,
        "automatic_error_cost": measured["automatic_rank"][0],
        "ranking": [
            round(cost, 10),
            duration["missed_presence_episodes"],
            duration["missed_seconds"],
            duration["false_positive_seconds"],
            *measured["automatic_rank"],
        ],
        "presence_recall": 1 - miss,
        "false_positive_percent": 100 * false,
        "target_met": 1 - miss >= MIN_RECALL - 1e-12,
        "basis": "human" if set(sources.values()) == {"human"} else "estimated",
    }


def _bounds(view, device_id, start, end):
    return spans.bounds(view, device_id, start=start, end=end)


RESOLUTION = 0.001  # Recorded times are kept to the millisecond: less is no time at all.
DEFAULT_THRESHOLD = 50  # A gate's starting point when its current threshold is unknown.


def fit(
    view,
    device_id,
    keys,
    current,
    timing=None,
    automatic_weight=DEFAULT_AUTOMATIC_WEIGHT,
    *,
    constraints=None,
    start=-inf,
    end=inf,
):
    start, end = _bounds(view, device_id, start, end)
    current = {key: (current or {}).get(key, DEFAULT_THRESHOLD) for key in keys}
    objective = _objective(
        view, device_id, keys, current, timing or {}, automatic_weight, constraints, start, end
    )
    policy = _selection(objective, view, keys, current, timing or {}, automatic_weight, constraints)
    _reference(objective, policy["selected"] == "reference", automatic_weight)
    thresholds = _search(objective)
    return _result(objective, thresholds, keys, timing or {}, policy, automatic_weight)


def _reference(objective, reference, weight):
    objective.reference = reference or weight == 0
    objective._sources(weight)
    objective.preferences = objective._member_preferences({}, None)
    radar = objective.radars[objective.device_id]
    objective.preferred = {
        objective.device_id: {
            key: min(
                radar.allowed[key],
                key=lambda value: (abs(value - entry["preferred_threshold"]), -value),
            )
            for key, entry in objective.preferences[objective.device_id].items()
        }
    }


def _support_lowerings(objective, thresholds):
    """Legal crossings of human occupied readings missed by an individual gate."""
    radar = objective.radars[objective.device_id]
    for key, threshold in thresholds.items():
        energies = {
            values[key]
            for _, _, values, state, source, _ in objective.regions
            if source == "human"
            and state == "present"
            and key in values
            and values[key] <= threshold
        }
        below = {
            max((v for v in radar.allowed[key] if v < energy), default=None) for energy in energies
        }
        yield from ((key, value) for value in sorted(v for v in below if v is not None))


def _support_pairs(objective, thresholds):
    """Restore one gate's preferred margin while another takes its occupied support."""
    preferred = objective.preferred[objective.device_id]
    raised = [key for key, value in thresholds.items() if value < preferred[key]]
    if not raised:
        return
    lowerings = list(_support_lowerings(objective, thresholds))
    for first in raised:
        for second, value in lowerings:
            if first != second:
                yield {**thresholds, first: preferred[first], second: value}


def _search(objective):
    """Coordinate search followed by bounded, strictly improving device support swaps."""
    key = objective.device_id
    thresholds = fitting.search(objective)[key]
    score = objective.rank({key: thresholds})
    for _ in range(4):
        previous = thresholds
        for candidate in _support_pairs(objective, previous):
            rank = objective.rank({key: candidate})
            if rank < score:
                thresholds, score = candidate, rank
        if thresholds is previous:
            break
    return thresholds


def _selection(objective, view, keys, current, timing, weight, constraints):
    model = objective.quality[objective.device_id]
    audit = {
        "selected": "corrected_positive",
        "validated": False,
        "reason": "no_valid_chronological_cut",
        "cutoff": None,
        "candidates": {},
        "quality_version": QUALITY_VERSION,
        "provenance": "recorded_basis_only",
        "model": model.audit(),
    }
    cutoff = chronological_split(model.context.episodes)
    if cutoff is None:
        return audit
    earlier = _objective(
        earlier_view(view, cutoff),
        objective.device_id,
        keys,
        current,
        timing,
        weight,
        constraints,
        objective.start,
        cutoff,
    )
    reports, empties = {}, []
    for policy in ("reference", "corrected_positive"):
        _reference(earlier, policy == "reference", weight)
        thresholds = _search(earlier)
        score = _measure(objective, thresholds, start=cutoff)["duration"]
        reports[policy] = {"thresholds": thresholds, "score": score}
        empties.append(
            [
                _measure(objective, thresholds, start=ep.start, end=ep.end)["duration"][
                    "false_positive_seconds"
                ]
                for ep in model.context.episodes
                if ep.state == "not_present" and ep.start >= cutoff
            ]
        )
    reference, positive = (item["score"] for item in reports.values())
    choice = select(
        reference,
        positive,
        zip(*empties, strict=True),
        measurable=all(reference[key] > 0 for key in ("present_seconds", "empty_seconds")),
    )
    audit.update(
        selected=choice.policy,
        validated=choice.validated,
        reason=choice.reason,
        cutoff=cutoff,
        candidates=reports,
        training_model=earlier.quality[objective.device_id].audit(),
    )
    return audit


def _automatic_evidence(objective, estimated, weight):
    samples = {
        state: estimated[key]
        for state, key in (("present", "present_samples"), ("not_present", "not_present_samples"))
    }
    effective = {
        state: value if not objective.reference or not objective.totals["human", state] else 0
        for state, value in samples.items()
    }
    confidences = {
        state: _mean_confidence(objective.regions, state) if value else None
        for state, value in effective.items()
    }
    return {
        "offered_samples": samples,
        "samples": effective,
        "used": any(effective.values()),
        "base_weight": weight,
        "mean_confidence": confidences,
        "share": dict(zip(evidence.LABELS, objective.shares, strict=True)),
        "priority": "human_first",
        "deferred_samples": 0,
    }


def _mean_confidence(regions, state):
    chosen = [
        (b - a, confidence)
        for a, b, _, label, source, confidence in regions
        if source == "automatic" and label == state
    ]
    elapsed = sum(duration for duration, _ in chosen)
    return (
        sum(duration * confidence for duration, confidence in chosen) / elapsed if elapsed else None
    )


def _result(objective, thresholds, keys, timing, policy, weight):
    training, estimated = (
        _measure(objective, thresholds, source) for source in ("human", "automatic")
    )
    counts = {
        state: training[key]
        for state, key in (("present", "present_samples"), ("not_present", "not_present_samples"))
    }
    status = _status(training, estimated)
    automatic = _automatic_evidence(
        objective, _measure(objective, thresholds, "automatic", effective=False), weight
    )
    basis = "mixed" if automatic["used"] else "human"
    if not any(counts.values()):
        basis = "automatic"
    observed = {key for region in objective.regions for key in keys if key in region[2]}
    groups, guessed = _sample_groups(objective, True), _sample_groups(objective, False)
    exclusive = exclusive_presence(groups["present"], thresholds)
    result = {
        "method": METHOD,
        "status": status,
        "counts": counts,
        "keys": list(keys),
        "training": training,
        "estimated_training": estimated,
        "warnings": [
            "These are observed training durations, not proof of field accuracy. Automatic "
            "confidence is heuristic; verify quiet presence and empty-room behaviour."
        ],
        "count_basis": "observed_seconds / 6",
        "evidence_basis": basis,
        "automatic_evidence": automatic,
        "proposals": {}
        if status == "insufficient"
        else {
            key: {
                "threshold": value,
                "status": status,
                "evidence_basis": basis,
                "auto_used": automatic["used"],
                "separation": objective.preferences[objective.device_id][key],
                "role": _gate_role(key in observed, value),
                **exclusive[key],
                **_gate_measures(key, value, groups, guessed),
            }
            for key, value in thresholds.items()
        },
        "timing": {"configuration": timing or {}, "model": "physical_interval_time"},
        "adaptive_policy": policy,
        "validation_scope": "whole_episode_chronological",
        "targets": TARGETS,
        "validation": None,
        "outlier_filter": {"method": "native_intervals_retained", "excluded": False},
        "feasibility": {
            "status": "not_assessed",
            "windows": [],
            "reason": "Native recommendations minimize finite elapsed-time error.",
        },
    }
    if status != "insufficient":
        result["review"] = review_evidence(groups, thresholds, timing)
    if timing.get("timeout") is not None:
        result["raw_training"] = _raw_training(objective, thresholds)
    _diagnostics(result, objective, thresholds, policy)
    failures = _failures(result, counts)
    result["warnings"] = [*failures, *_notes(result, basis), *result["warnings"]]
    for proposal in result["proposals"].values():
        proposal["message"] = "; ".join(failures)
    _budget_result(result, objective)
    return result


def _budget_result(result, objective):
    if objective.plan is None:
        return
    result["evidence_budget"] = {
        **objective.plan.audit,
        "episode_counts": "original_independent_quality_episodes",
        "diagnostics": "selected_recorded_periods",
    }
    result["count_basis"] = "represented_observed_seconds / 6"
    if objective.plan.status == "unsupported":
        result.update(status="insufficient", proposals={})
        reason = (
            "The configured timing context cannot fit the learning evidence budget."
            if objective.plan.audit.get("reason") == "timing_context_exceeds_budget"
            else "No labelled observed time is available for the learning evidence budget."
        )
        result["warnings"].append(reason)
        return
    result["warnings"].append(
        "Learning used representative recorded periods. Time totals are weighted estimates; "
        "episode, burst and longest-miss diagnostics describe the selected periods."
    )


def _raw_training(objective, thresholds):
    """The same labelled physical trace before device hold and delay filters."""
    raw = copy(objective)
    raw._measured_targets = {}  # Its own radar's coverage: never the filtered one's targets.
    key = objective.device_id
    raw.radars = {
        key: Radar(
            {"thresholds": thresholds, "timing": {}},
            objective.physical_inputs,
            objective.quality[key],
        )
    }
    return _measure(raw, thresholds)


def _failures(result, counts):
    """Why the recommendation misses its targets, as the learner has always explained it."""
    training = result["training"]
    failures = _human_failures(training)
    if (training["duration"]["false_positive_percent"] or 0) >= 100 - 1e-9:
        failures.append("This candidate is active throughout the observed empty-room time")
    failures.extend(
        "Recent human labels: " + failure for failure in _human_failures(result["recent_training"])
    )
    estimated = result["estimated_training"]["duration"]
    recall = estimated["presence_recall"]
    if not counts["present"] and recall is not None and recall < MIN_RECALL - 1e-12:
        failures.append(
            f"Estimated presence-time recall from automatic labels is {recall:.3%}; "
            f"target {MIN_RECALL:.1%}"
        )
    if not counts["not_present"] and (estimated["false_positive_percent"] or 0) >= 100 - 1e-9:
        failures.append("This candidate is active throughout automatically labelled empty time")
    return failures


def _human_failures(measured):
    """Human-target failures of a measurement (its presence-time ``duration``)."""
    duration = measured["duration"]
    failures = []
    recall = duration["presence_recall"]
    if recall is not None and recall < MIN_RECALL - 1e-12:
        failures.append(f"Estimated presence-time recall {recall:.3%}; target {MIN_RECALL:.1%}")
    if duration["missed_presence_episodes"]:
        failures.append(
            f"{duration['missed_presence_episodes']} occupied periods have no detection"
        )
    return failures


TARGETS = {  # What a recommendation is measured against (the panel reads ``sensitivity``).
    "sensitivity": MIN_RECALL,
    "scoring": "weighted_post_timing_error",
    "missed_time_cost": MISSED_TIME_COST,
    "missed_presence_episodes": 0,
    "false_positive_score": "negative_percent_of_observed_empty_time",
}
STATUS_NOTES = {
    "uncertain": (
        "Some observations have unresolved timing or no measurable duration. The "
        "presence-time target is not fully confirmed.",
    ),
    "tradeoff": (
        "Presence-time target met; false-positive time is reported as a penalty, not a failed "
        "learning job.",
    ),
}


def _notes(result, basis):
    notes = []
    if basis != "human":
        notes.append(
            "Inferred observations contribute with lower confidence; their proportion does not "
            "block Apply. Human-labelled results always take priority."
        )
    notes.extend(STATUS_NOTES.get(result["status"], ()))
    if result["validation"] and _human_failures(result["validation"]):
        notes.append(
            "Earlier-data backtest missed its targets; the final recommendation was refit using "
            "all retained observations. Test it in new sessions."
        )
    return notes


def _sample_groups(objective, human):
    """Labelled time as the samples the sampled learner kept, for the review and the per-gate
    measures: one every ``SAMPLE_SECONDS`` through each region labelled by a person
    (``human``) or else automatically, the newest ``MAX_CLASS_SAMPLES`` of each class, oldest
    first."""
    groups = {"present": [], "not_present": []}
    for low, high, readings, state, source, *_ in reversed(objective.regions):
        rows = groups.get(state)
        if (source == "human") != human or rows is None or len(rows) >= MAX_CLASS_SAMPLES:
            continue
        # Samples on one clock (whole multiples of SAMPLE_SECONDS), so how a recorded span is
        # divided never changes them; the newest first.
        first, after = ceil(low / SAMPLE_SECONDS), ceil(high / SAMPLE_SECONDS)
        for step in range(after - 1, max(first, after - MAX_CLASS_SAMPLES + len(rows)) - 1, -1):
            rows.append((step * SAMPLE_SECONDS, readings, state))
    return {state: rows[::-1] for state, rows in groups.items()}


def _gate_measures(key, threshold, groups, guessed):
    """One gate's own measures at ``threshold`` on the labelled samples, and the empty-room
    readings it must stay above (human-labelled, else automatic)."""
    measured = metrics(groups["present"] + groups["not_present"], {key: threshold})
    manual = [row[1][key] for row in groups["not_present"] if key in row[1]]
    noise = sorted(manual or [row[1][key] for row in guessed["not_present"] if key in row[1]])
    return {
        **measured,
        "message": "",
        "specificity": 1 - measured["false_positive_rate"],
        "false_negative_rate": 1 - measured["sensitivity"],
        "noise_ceiling": noise[-1] if noise else None,
        "noise_floor_p99": noise[int((len(noise) - 1) * 0.99)] if noise else None,
        "noise_source": "manual" if manual else "automatic",
        "safety_margin": threshold - noise[-1] if noise else None,
    }


def _gate_role(observed, threshold):
    """``unchanged``: the gate never read, so its threshold is kept; else whether it detects."""
    if not observed:
        return "unchanged"
    return "suppressed" if threshold == 100 else "detection"


PRESENCE_FIELDS = (
    "present_seconds",
    "missed_seconds",
    "missed_seconds_upper",
    "presence_recall",
    "presence_recall_lower",
    "presence_episodes",
    "missed_presence_episodes",
    "unscored_presence_samples",
    "longest_missed_seconds",
    "longest_missed_seconds_upper",
)


def _class_start(objective, state):
    """Where the newest 20% of the human-labelled ``state`` time begins."""
    regions = [r for r in objective.regions if r[3] == state and r[4] == "human"]
    remaining = sum(_seconds(objective, [(low, high)]) for low, high, *_ in regions) * 0.2
    for low, high, *_ in reversed(regions):
        mass = _seconds(objective, [(low, high)])
        if remaining <= mass and mass > 0:
            if objective.plan is None:
                return high - remaining
            return high - remaining * (high - low) / mass
        remaining -= mass
    return objective.start


def _recent(objective, thresholds):
    """The recommendation on the newest 20% of each human class: presence measured from where
    the newest presence time begins, empty time from where the newest empty time begins."""
    presence = _measure(objective, thresholds, start=_class_start(objective, "present"))
    result = _measure(objective, thresholds, start=_class_start(objective, "not_present"))
    duration = result["duration"]
    duration.update({key: presence["duration"][key] for key in PRESENCE_FIELDS})
    recall, empty = duration["presence_recall"], duration["empty_seconds"]
    false = duration["false_positive_seconds"] / empty if empty else 0
    duration["error_cost"] = error_cost(1 - recall if recall is not None else 0, false)
    for key in (
        "present_samples",
        "sensitivity",
        "false_negatives",
        "presence_episodes",
        "missed_presence_episodes",
        "longest_missed_run_samples",
    ):
        result[key] = presence[key]
    return result


def _diagnostics(result, objective, thresholds, policy):
    result["recent_training"] = _recent(objective, thresholds)
    if policy["cutoff"] is not None:
        earlier = policy["candidates"][policy["selected"]]["thresholds"]
        result["validation"] = _measure(objective, earlier, start=policy["cutoff"])
        result["current_validation"] = _measure(
            objective, objective.current[objective.device_id], start=policy["cutoff"]
        )


def _status(training, estimated):
    if any(
        (training[key] or estimated[key]) < MIN_CLASS_SAMPLES
        for key in ("present_samples", "not_present_samples")
    ):
        return "insufficient"
    presence = training if training["present_samples"] else estimated
    empty = training if training["not_present_samples"] else estimated
    if (
        _presence_failure(presence)
        or (empty["duration"]["false_positive_percent"] or 0) >= 100 - 1e-9
    ):
        return "unsafe"
    return (
        "tradeoff"
        if any(
            report["duration"]["false_positive_seconds"] > RESOLUTION
            for report in (training, estimated)
        )
        else "ok"
    )


def _presence_failure(report):
    duration = report["duration"]
    return duration["missed_presence_episodes"] or (
        duration["presence_recall"] is not None and duration["presence_recall"] < MIN_RECALL
    )
