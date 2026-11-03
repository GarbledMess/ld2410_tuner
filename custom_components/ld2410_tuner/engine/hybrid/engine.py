"""One hybrid fit: evidence horizon, cached-cell search, common-fidelity scoring, selection.

``fit`` applies the evidence horizons, folds training cells only from days that
intersect the label regions, searches candidates (plus quiet-presence candidates
when occupied evidence is short), scores every compared pattern at one fidelity
with its quiet-presence pass, and selects with the evidence band and guards.
Reported ``before``/``after`` are pure training error costs; the quiet prior and
selection costs are separate diagnostics. Temporary files never outlive the fit.
"""

import tempfile
from bisect import bisect_right
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import anomaly, selection
from .constants import ROBUST_SEARCH_MIN_WEIGHT, ROBUST_SHORTLIST, ROBUST_SWEEPS
from .domains import normalize_pattern
from .evaluate import Evaluators, evaluate_patterns, plan_scores, score_regions
from .evidence import evidence_horizon
from .search import _unique, collect_training, quiet_candidates, run_search
from .search_data import pure_cells
from .training import TrainingStore
from .types import FitResult

STATES = ("present", "not_present")


class LabelDays:
    """The fit's day source restricted to days that intersect its label regions."""

    def __init__(self, days, regions):
        self.days = days
        self.starts = [a for a, _ in regions]
        self.ends = [b for _, b in regions]
        bounds = getattr(days, "bounds", None)
        if bounds is not None:
            self.bounds = bounds

    def _wanted(self, date):
        bounds = getattr(self, "bounds", None)
        if bounds is None:
            return True
        lo, hi = bounds(date)
        i = bisect_right(self.ends, lo)
        return i < len(self.starts) and self.starts[i] < hi

    def dates(self, device_id):
        return (date for date in self.days.dates(device_id) if self._wanted(date))

    def digest(self, device_id, date, channels):
        return self.days.digest(device_id, date, channels)


def with_horizon(problem):
    """Each zone's truth after the automatic and human evidence horizons."""
    zones = tuple(
        replace(z, truth=evidence_horizon(z.truth, problem.end, problem.policy))
        for z in problem.zones
    )
    return replace(problem, zones=zones)


def prepared(problem):
    """Horizon-clipped truth, read only within the hull of the label regions.

    Regions already include the timing warm-up, so nothing outside the hull is
    labelled or within timing reach: narrowing the window changes no score.
    """
    problem = with_horizon(problem)
    regions = score_regions(problem)
    if regions:
        problem = replace(problem, start=regions[0][0], end=regions[-1][1])
    return replace(problem, days=LabelDays(problem.days, regions))


@dataclass
class FitState:
    """Everything a later masked re-score reuses: candidates, plan and records."""

    problem: object
    training: object
    directory: Path
    start: dict
    engine: object = None
    patterns: list = field(default_factory=list)
    plan: object = None
    evaluators: object = None
    scores: list = field(default_factory=list)
    quiet: list = field(default_factory=list)
    compared: list = field(default_factory=list)
    quiet_search: bool = False


def search_candidates(state):
    """Start-first finalists, plus quiet candidates when the prior weight is high."""
    problem, training = state.problem, state.training
    cells = pure_cells(training)
    if not all(cells.get(s) for s in STATES):
        return [state.start]
    state.engine, patterns = run_search(problem, training)
    weights = selection.weights(problem)
    state.quiet_search = max(weights.values(), default=0) > ROBUST_SEARCH_MIN_WEIGHT
    if state.quiet_search:
        extra = quiet_candidates(
            problem, training, state.engine, sweeps=ROBUST_SWEEPS, shortlist=ROBUST_SHORTLIST
        )
        patterns = _unique([*patterns, *extra], state.engine.coordinates)
    return patterns


def score(state, patterns, *, zones=None, keep=1):
    """Common-fidelity base and quiet scores; returns (indexes, base, quiet)."""
    problem = state.problem

    def rank(value):
        return selection.selection_costs(problem, [value], [value["quiet"]])[0][0]

    scores = evaluate_patterns(
        problem,
        patterns,
        state.plan,
        quiet=True,
        rank=rank,
        evaluators=state.evaluators,
        zones=zones,
        keep=keep,
        attribution=not _defer_attribution(state),
    )
    base = [{k: v for k, v in s.items() if k != "quiet"} for s in scores]
    return [s["pattern_index"] for s in scores], base, [s["quiet"] for s in scores]


def _totals(metrics):
    zones = metrics["zones"].values()
    return {
        key: sum(z[key] for z in zones)
        for key in ("occupied_seconds", "empty_seconds", "occupied_episodes", "empty_periods")
    }


def _human_seconds(problem):
    return sum(
        max(0.0, min(s.end, problem.end) - max(s.start, problem.start))
        for z in problem.zones
        for s in z.truth
        if s.source == "human" and s.state in STATES
    )


def evidence(state):
    policy = state.problem.policy
    return {
        **_totals(state.scores[0]),
        "human_seconds": _human_seconds(state.problem),
        "pure_cells": pure_cells(state.training),
        "automatic_horizon_days": policy.automatic_horizon_days,
        "human_evidence_hours": policy.human_evidence_hours,
    }


def first_fit(state):
    """Search, plan once for every compared pattern, score, and select."""
    state.patterns = search_candidates(state)
    state.plan = plan_scores(state.problem, len(state.patterns), quiet=True)
    state.evaluators = Evaluators(state.problem, state.plan, directory=state.directory)
    indexes, state.scores, state.quiet = score(state, state.patterns)
    state.compared = [state.patterns[i] for i in indexes]
    totals = _totals(state.scores[0])
    missing = [
        s
        for s, key in zip(STATES, ("occupied_seconds", "empty_seconds"), strict=True)
        if not totals[key]
    ]
    if missing:
        return 0, {"why": "insufficient evidence"}, missing
    stats = state.engine.stats if state.engine is not None else {}
    pick, info = selection.choose(state.problem, state.compared, state.scores, state.quiet, stats)
    return pick, info, []


def _defer_attribution(state):
    return any(len(zone.members) > 1 for zone in state.problem.zones)


def _reported_scores(state, pick):
    """Hydrate only report metrics from vectors already present in replay records."""
    if not _defer_attribution(state):
        return state.scores[0], state.scores[pick]
    indexes = tuple(dict.fromkeys((0, pick)))
    mode = "cells" if state.plan.mode == "cells" else "exact"
    patterns = [state.compared[i] for i in indexes]
    scores = state.evaluators.get(mode).scores(patterns, zones=state.problem.zones)
    reported = {i: {**state.scores[i], **score} for i, score in zip(indexes, scores, strict=True)}
    return reported[0], reported[pick]


def result(state, pick, info, missing, extra=None):
    """FitResult with pure error costs and separately reported selection terms."""
    costs = selection.selection_costs(state.problem, state.scores, state.quiet)
    before, after = _reported_scores(state, pick)
    status = "insufficient" if missing else ("fitted" if pick else "kept")
    diagnostics = {
        "status": status,
        "scoring_mode": state.plan.mode,
        "score_plan": state.plan,
        "decision": info,
        "finalists": len(state.patterns),
        "scored": len(state.scores),
        "quiet_search": state.quiet_search,
        "quiet_weight": selection.weights(state.problem),
        "baseline_cost": costs[0][0],
        "base_cost": selection.base_cost(after),
        "quiet_cost": costs[pick][1],
        "selection_cost": costs[pick][0],
        "row_work": state.plan.row_count,
        "operations": state.evaluators.operations,
        "spilled_bytes": state.evaluators.spilled_bytes,
        "evidence": evidence(state),
    }
    if missing:
        diagnostics["insufficient_classes"] = missing
    diagnostics.update(extra or {})
    thresholds = {rid: dict(v) for rid, v in state.compared[pick].items()}
    return FitResult(thresholds, before, after, diagnostics)


def _dedupe(patterns):
    seen, out = set(), []
    for pattern in patterns:
        key = tuple((rid, tuple(sorted(v.items()))) for rid, v in sorted(pattern.items()))
        if key not in seen:
            seen.add(key)
            out.append(pattern)
    return out


def _rescore_patterns(state, masked, found, first):
    """Fit 1's start, choice and candidates plus closed-form masked gap centres."""
    quiet = max(selection.weights(masked).values(), default=0) > ROBUST_SEARCH_MIN_WEIGHT
    seeds, stats = anomaly.masked_seeds(
        masked, state.training, found.masks, state.start, quiet=quiet
    )
    return _dedupe([state.start, first, *state.patterns, *seeds]), stats


def _masked_fit(state, found, first):
    """Re-score fit 1's shortlist with suspects masked (no search, same fidelity)."""
    masked = anomaly.masked_problem(state.problem, found.masks)
    patterns, stats = _rescore_patterns(state, masked, found, first)
    work = anomaly.operations(state.problem, state.plan, len(patterns))
    if not anomaly.within_budget(work):
        return None, work, None, None
    second = replace(state, problem=masked, patterns=patterns, quiet_search=state.quiet_search)
    indexes, second.scores, second.quiet = score(second, patterns, zones=masked.zones, keep=2)
    second.compared = [patterns[i] for i in indexes]
    pick, info = selection.choose(masked, second.compared, second.scores, second.quiet, stats)
    return second, work, pick, info


def _gain(second, pick, first):
    costs = selection.selection_costs(second.problem, second.scores, second.quiet)
    at = second.compared.index(first) if first in second.compared else 0
    return costs[at][0] - costs[pick][0]


def audited(state, first, pick):
    """Fit 1 unless masking audited suspects improves selection beyond the band."""
    found = anomaly.audit(state.problem, state.training)
    if not found.accepted:
        first.diagnostics["anomaly"] = anomaly.report(found, "clean")
        return first
    second, work, choice, info = _masked_fit(state, found, first.thresholds)
    if second is None:
        status = "kept: not tested (operation budget)"
        first.diagnostics["anomaly"] = anomaly.report(found, status, work=work)
        return first
    totals = _totals(second.scores[0])
    gain = _gain(second, choice, first.thresholds)
    margin = anomaly.masked_margin(second.scores[0])
    measured = {"gain": round(gain, 6), "margin": round(margin, 6), "work": work}
    if totals["occupied_seconds"] and totals["empty_seconds"] and gain > margin:
        summary = {
            "status": first.diagnostics["status"],
            "thresholds": first.thresholds,
            "base_cost": first.diagnostics["base_cost"],
            "selection_cost": first.diagnostics["selection_cost"],
        }
        report = anomaly.report(found, "discarded", **measured)
        # Reports re-measure on the same masked truth: discarded time stays unobserved.
        report["discarded_intervals"] = {
            gid: [list(span) for span in spans] for gid, spans in found.masks.items()
        }
        extra = {"anomaly": report, "first_fit": summary}
        return result(second, choice, info, [], extra)
    first.diagnostics["anomaly"] = anomaly.report(found, "kept: no influence", **measured)
    return first


def fit(problem, *, audit=True):
    """Fit one shared threshold vector per radar for every zone of ``problem``.

    With ``audit`` (the production default) suspect label stretches are masked
    and fit 1's candidates re-scored; the masked choice replaces fit 1 only when
    its selection gain on the retained labels exceeds their evidence band.
    """
    problem = prepared(problem)
    with tempfile.TemporaryDirectory(prefix="hybrid-fit-") as directory:
        directory = Path(directory)
        with TrainingStore(directory) as training:
            collect_training(problem, training)
            state = FitState(problem, training, directory, normalize_pattern(problem.radars))
            try:
                pick, info, missing = first_fit(state)
                first = result(state, pick, info, missing)
                if missing or not audit:
                    return first
                return audited(state, first, pick)
            finally:
                if state.evaluators is not None:
                    state.evaluators.close()
