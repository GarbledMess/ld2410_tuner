"""Adaptive evidence policy: two training-only fits scored on later explicit episodes.

Both candidate policies are fitted by the same hybrid engine on the earlier
history; identical policies (no automatic weight) share one search. Later human
episodes are scored with the shared evaluator, so the held-out cost is the
common time-and-episode cost. The conservative duration and per-empty-episode
harm guards of ``calibration.selection.select`` still decide the policy.
"""

from collections import defaultdict
from dataclasses import replace

from ...calibration.selection import select
from . import engine, fixed
from .types import ZoneSpec

POLICIES = ("reference", "corrected_positive")
COUNTS = (
    "occupied_episodes",
    "empty_periods",
    "mostly_missed_episodes",
    "empty_periods_mostly_false_active",
    "empty_periods_failed",
)


def reference_flag(policy, weight):
    """Without automatic weight the two policies are the same evidence policy."""
    return policy == "reference" or weight == 0


def with_reference(problem, reference):
    return replace(problem, policy=replace(problem.policy, reference=reference))


def training_fits(training, weight, fit=None):
    """``{policy: pattern}``, searching once per distinct evidence policy."""
    fit = engine.fit if fit is None else fit
    searched, found = {}, {}
    with engine.FitSession() as session:
        for policy in POLICIES:
            flag = reference_flag(policy, weight)
            if flag not in searched:
                searched[flag] = fit(with_reference(training, flag), session=session).thresholds
            found[policy] = searched[flag]
    return found


def _held_zones(problem, cutoff):
    """Per zone: later human truth, and one zone per later empty episode."""
    cut = dict.fromkeys(fixed.STATES, cutoff)
    held, empty = [], []
    for zone in problem.zones:
        later = fixed.since(fixed.source(zone, "human"), "held", cut)
        held.append(later)
        empty.extend(_empty_episodes(later))
    return held, empty


def _empty_episodes(zone):
    pieces = defaultdict(list)
    for span in zone.truth:
        if span.state == "not_present":
            pieces[span.episode_id].append(span)
    return [
        ZoneSpec(fixed.tag(zone.group_id, "episode", str(i)), zone.members, tuple(spans))
        for i, spans in enumerate(pieces.values())
    ]


def _unique(patterns):
    found = []
    for pattern in patterns:
        if pattern not in found:
            found.append(pattern)
    return found


def held_score(metrics):
    """Summed later-episode measures: legacy duration fields plus the common cost."""
    # A zone without later time in one class has no cost; the others still compare.
    costs = [m["cost"] for m in metrics if m["cost"] is not None]
    score = {
        "present_seconds": sum(m["occupied_seconds"] for m in metrics),
        "empty_seconds": sum(m["empty_seconds"] for m in metrics),
        "missed_seconds": sum(fixed.missed_seconds(m) for m in metrics),
        "false_seconds": sum(m["false_active_seconds"] for m in metrics),
        "missed_episodes": sum(m["completely_missed_episodes"] for m in metrics),
        "cost": sum(costs) if costs else None,
    }
    score.update({key: sum(m[key] for m in metrics) for key in COUNTS})
    return score


def score_candidates(problem, cutoff, candidates, named, session=None):
    """``(reports, empties)``: each policy's thresholds, held score and empty harms."""
    problem = engine.with_horizon(problem)
    held, empty = _held_zones(problem, cutoff)
    patterns = _unique(candidates.values())
    with fixed.FixedScores(problem, len(patterns), session=session) as scores:
        found = scores.scores(patterns, [*held, *empty])
    reports, empties = {}, []
    for policy, pattern in candidates.items():
        measured = found[patterns.index(pattern)]
        score = held_score([measured[z.group_id] for z in held])
        reports[policy] = {"thresholds": named(pattern), "score": score}
        empties.append([measured[z.group_id]["false_active_seconds"] for z in empty])
    return reports, empties


def decide(reports, empties):
    reference, positive = (reports[policy]["score"] for policy in POLICIES)
    measurable = reference["present_seconds"] > 0 and reference["empty_seconds"] > 0
    return select(reference, positive, zip(*empties, strict=True), measurable=measurable)
