"""Panel report adapters over evaluator-owned aggregates.

No search, replay or cost reconstruction lives here. FitResult.diagnostics carries
``measurements`` (device) / ``zones[gid]`` (room): human, automatic, raw, recent
metric dictionaries, plus member metrics/Seen-by tallies for room assessments.
Each metric dictionary follows SliceScorer.result; raw/recent/source evaluations
must be supplied by the fixed-pattern evaluator using one common ScorePlan.
Missing aggregates are errors, never fabricated reports of zero error.
"""

from copy import deepcopy
from dataclasses import replace

from ...calibration.constants import MIN_RECALL
from ...const import HISTORY_KEYS
from .diagnostics import diagnostics
from .types import FitProblem, FitResult

TARGETS = {
    "sensitivity": MIN_RECALL,
    "scoring": "weighted_post_timing_time_and_episode_error",
    "missed_time_cost": 5,
    "missed_presence_episodes": 0,
    "false_positive_score": "negative_percent_of_observed_empty_time",
}
STATES = ("present", "not_present")
EXTRA = (
    "mostly_missed_episodes",
    "empty_periods_failed",
    "empty_periods_with_false_trigger",
    "empty_periods_mostly_false_active",
    "false_triggers",
    "time_term",
    "episode_term",
)


def _required(bundle, key):
    if key not in bundle:
        raise ValueError(f"Missing evaluator measurement: {key}")
    return bundle[key]


def _bundle(result, group_id=None):
    values = result.diagnostics
    bundle = values["measurements"] if group_id is None else values["zones"][group_id]
    for key in ("human", "automatic", "raw", "recent"):
        if key not in bundle:
            raise ValueError(f"Missing evaluator measurement: {key}")
    return bundle


def measurement(metrics, mode):
    """Preserve seconds, six-second sample equivalents and negative FPR score."""
    p, e = metrics["occupied_seconds"], metrics["empty_seconds"]
    recall = metrics["occupied_recall"]
    missed = p * (1 - recall) if recall is not None else 0
    false = metrics["false_active_seconds"]
    percent = 100 * false / e if e else None
    longest = metrics["longest_miss_seconds"]
    episodes = metrics["occupied_episodes"]
    complete = metrics["completely_missed_episodes"]
    triggers = metrics["false_triggers"]
    duration = {
        "present_seconds": p,
        "empty_seconds": e,
        "missed_seconds": missed,
        "missed_seconds_upper": missed,
        "false_positive_seconds": false,
        "false_positive_percent": percent,
        "false_positive_score": -percent if percent is not None else None,
        "presence_recall": recall,
        "presence_recall_lower": recall,
        "presence_episodes": episodes,
        "missed_presence_episodes": complete,
        "false_trigger_events": triggers,
        "unscored_presence_samples": 0,
        "unscored_empty_samples": 0,
        "basis": "six_second_cells" if mode == "cells" else "physical_interval_time",
        "missed_time_cost": 5,
        "longest_missed_seconds": longest,
        "longest_missed_seconds_upper": longest,
        "error_cost": metrics["cost"],
    }
    return dict(
        duration=duration,
        present_samples=p / 6,
        not_present_samples=e / 6,
        sensitivity=recall,
        false_positive_rate=false / e if e else None,
        false_negatives=missed / 6,
        false_positives=false / 6,
        presence_episodes=episodes,
        missed_presence_episodes=complete,
        longest_missed_run_samples=longest / 6,
        false_trigger_bursts=triggers,
        false_trigger_bursts_per_hour=triggers * 3600 / e if e else None,
        observed_absent_seconds=e,
        **{key: metrics[key] for key in EXTRA},
    )


def _mode(result):
    mode = result.diagnostics["scoring_mode"]
    if mode not in ("exact", "cells_then_exact", "cells"):
        raise ValueError("Unknown scoring mode")
    return mode


def _status(metrics):
    if not metrics["occupied_seconds"] or not metrics["empty_seconds"]:
        return "insufficient"
    if metrics["completely_missed_episodes"] or metrics["occupied_recall"] < MIN_RECALL:
        return "unsafe"
    if metrics["false_active_seconds"] >= metrics["empty_seconds"]:
        return "unsafe"
    return "tradeoff" if metrics["false_active_seconds"] > 6 else "ok"


def _quantile(hist, q):
    target, total = sum(hist) * q, 0
    if not target:
        return None
    for value, mass in enumerate(hist):
        total += mass
        if total >= target:
            return value
    return None


def _gate_hist(hist, state):
    human = hist["human"][state]
    return human if sum(human) else hist["automatic"][state]


def _role(observed, threshold):
    if not observed:
        return "unchanged"
    return "suppressed" if threshold == 100 else "detection"


def _gate_proposal(key, channel, threshold, diag, status, basis, auto):
    hist = diag["histograms"][channel]
    present, empty = (_gate_hist(hist, s) for s in STATES)
    p, e = sum(present), sum(empty)
    missed, false = sum(present[: threshold + 1]), sum(empty[threshold + 1 :])
    ceiling = next((v for v in range(100, -1, -1) if empty[v]), None)
    floor = _quantile(present, 0.25)
    separated = ceiling is not None and floor is not None and ceiling < floor
    return {
        "threshold": threshold,
        "status": status,
        "evidence_basis": basis,
        "auto_used": auto,
        "message": "Review observed training performance and quiet presence.",
        "role": _role(p + e, threshold),
        "separation": {
            "preferred_threshold": (ceiling + floor) // 2 if separated else threshold,
            "noise_ceiling": ceiling,
            "presence_reference": floor,
            "separated": separated,
            "human_supported": bool(sum(hist["human"]["present"])),
        },
        "exclusive_presence_samples": _exclusive(diag, key, threshold),
        "weakest_exclusive_presence_energy": _weakest(diag, key, threshold),
        "present_samples": p / 6,
        "not_present_samples": e / 6,
        "sensitivity": 1 - missed / p if p else None,
        "false_positive_rate": false / e if e else None,
        "false_negatives": missed / 6,
        "false_positives": false / 6,
        "specificity": 1 - false / e if e else None,
        "false_negative_rate": missed / p if p else None,
        "noise_ceiling": ceiling,
        "noise_floor_p99": _quantile(empty, 0.99),
        "noise_source": "human" if sum(hist["human"]["not_present"]) else "automatic",
        "safety_margin": min(threshold - ceiling, floor - threshold) if separated else None,
    }


def _same_evidence(timed, raw):
    for key in ("occupied_seconds", "empty_seconds", "covered_seconds"):
        if abs(timed[key] - raw[key]) > 1e-7:
            raise ValueError("Raw/timed measurements must use the same observed evidence")


def _exclusive_gates(diag, thresholds):
    diag["thresholds"] = {HISTORY_KEYS[c]: t for c, t in thresholds.items()}


def _only_gate(energies, thresholds, key):
    detecting = [gate for gate, t in thresholds.items() if energies.get(gate, 0) > t]
    return detecting == [key]


def _exclusive_rows(diag, key, threshold):
    for row in diag["samples"]["present"]:
        energies = row["energies"]
        if energies.get(key, 0) > threshold and _only_gate(energies, diag["thresholds"], key):
            yield energies[key]


def _exclusive(diag, key, threshold):
    return sum(1 for _ in _exclusive_rows(diag, key, threshold))


def _weakest(diag, key, threshold):
    return min(_exclusive_rows(diag, key, threshold), default=None)


def _collected(problem, result):
    if "raw_diagnostics" in result.diagnostics:
        return result.diagnostics["raw_diagnostics"]
    plan = _required(result.diagnostics, "score_plan")
    if plan.mode != _mode(result):
        raise ValueError("score_plan must match evaluation scoring_mode")
    return diagnostics(problem, result.thresholds, plan)


def device_report(problem: FitProblem, result: FitResult, policy: dict) -> dict:
    """Build a device report from full-coverage fixed-pattern aggregates."""
    if len(problem.radars) != 1:
        raise ValueError("Device report requires one radar")
    bundle, mode = _bundle(result), _mode(result)
    _same_evidence(bundle["human"], bundle["raw"])
    collected = _collected(problem, result)
    device_id = next(iter(problem.radars))
    diag = deepcopy(collected["radars"][device_id])
    _exclusive_gates(diag, result.thresholds[device_id])
    training, estimated = (measurement(bundle[s], mode) for s in ("human", "automatic"))
    status, automatic = _status(result.after), deepcopy(_required(bundle, "automatic_evidence"))
    basis = "mixed" if automatic["used"] else "human"
    if not training["present_samples"] and not training["not_present_samples"]:
        basis = "automatic"
    report = {
        "method": "human_priority_v12",
        "status": status,
        "counts": {
            s: training["present_samples" if s == "present" else "not_present_samples"]
            for s in STATES
        },
        "keys": [HISTORY_KEYS[c] for c in problem.radars[device_id].channels],
        "warnings": _warnings(mode),
        "count_basis": "observed_seconds / 6",
        "evidence_basis": basis,
        "training": training,
        "estimated_training": estimated,
        "recent_training": measurement(bundle["recent"], mode),
        "raw_training": measurement(bundle["raw"], mode),
        "validation": None,
        "current_validation": None,
        "targets": deepcopy(TARGETS),
        "validation_scope": "whole_episode_chronological",
        "timing": {
            "configuration": problem.radars[device_id].timing,
            "model": training["duration"]["basis"],
        },
        "adaptive_policy": deepcopy(policy),
        "automatic_evidence": automatic,
        "review": deepcopy(diag["review"]),
        "outlier_filter": {"method": "native_intervals_retained", "excluded": False},
        "feasibility": {
            "status": "not_assessed",
            "windows": [],
            "reason": "Finite time and episode cost.",
        },
        "evidence_budget": deepcopy(collected["evidence_budget"]),
        "proposals": {},
    }
    report["review"]["influence"] = deepcopy(_required(bundle, "influence"))
    report.update(_selection(result))
    _validation(report, bundle, mode, policy)
    _anomaly(report, result.diagnostics.get("anomaly"))
    if status != "insufficient":
        report["proposals"] = {
            HISTORY_KEYS[c]: _gate_proposal(
                HISTORY_KEYS[c], c, t, diag, status, basis, automatic["used"]
            )
            for c, t in result.thresholds[device_id].items()
        }
    return report


def _validation(report, bundle, mode, policy):
    if policy.get("cutoff") is not None:
        for key in ("validation", "current_validation"):
            if key not in bundle:
                raise ValueError(f"Missing evaluator measurement: {key}")
            report[key] = measurement(bundle[key], mode)


def _selection(result):
    return {
        "base_cost": result.after["cost"],
        "quiet_cost": result.diagnostics["quiet_cost"],
        "selection_cost": result.diagnostics["selection_cost"],
        "scoring_mode": _mode(result),
    }


def _warnings(mode):
    warnings = [
        "Observed training performance is not proof of field accuracy. Verify quiet presence."
    ]
    if mode == "cells":
        warnings.append(
            "Timing and trigger diagnostics use six-second cell maxima; subcell events are approximate."
        )
    return warnings


def _anomaly(report, audit):
    if audit is not None:
        report["anomaly"] = deepcopy(audit)
        if "review" in report:
            report["review"]["excluded_automatically"] = bool(audit["discarded_count"])


def _outcome(metrics, source, state):
    seconds = metrics["occupied_seconds" if state == "present" else "empty_seconds"]
    active = (
        seconds * (metrics["occupied_recall"] or 0)
        if state == "present"
        else metrics["false_active_seconds"]
    )
    stats = metrics["sufficient_statistics"].get(state + "/" + source, [0] * 4)
    weighted_active = stats[0] - stats[1] if state == "present" else stats[1]
    return {
        "seconds": seconds,
        "active_seconds": active,
        "possible_seconds": active,
        "weighted_seconds": stats[0],
        "weighted_active_seconds": weighted_active,
        "samples": seconds / 6,
        "hits": active / 6,
    }


def _assessment(metrics, bundle, mode):
    outcomes = {
        src: {s: _outcome(bundle[src], src, s) for s in STATES} for src in ("human", "automatic")
    }
    sources = {
        s: next((src for src in ("human", "automatic") if outcomes[src][s]["seconds"]), None)
        for s in STATES
    }
    recall = metrics["occupied_recall"]
    empty = metrics["empty_seconds"]
    return dict(
        outcomes=outcomes,
        sources=sources,
        score=metrics["score"],
        error_cost=metrics["cost"],
        reason=None if metrics["cost"] is not None else "No observed labelled time in one class.",
        presence_recall=recall,
        false_positive_percent=100 * metrics["false_active_seconds"] / empty if empty else None,
        target_met=recall is not None and recall >= MIN_RECALL,
        basis="six_second_cells" if mode == "cells" else "physical_interval_time",
        missed_time_cost=5,
        **{key: metrics[key] for key in EXTRA},
    )


def _member_sources(member):
    if "sources" not in member:
        raise ValueError("Missing member sources measurement")
    return member["sources"]


def _room_view(problem, metrics, bundle, mode):
    members = {}
    for device_id, member in bundle["members"].items():
        member_metrics = member["metrics"]
        members[device_id] = {
            **_assessment(member_metrics, _member_sources(member), mode),
            **{
                key: member[key]
                for key in (
                    "exclusive_presence_seconds",
                    "missed_seen_seconds",
                    "missed_seen_inferred_seconds",
                    "missed_seen_automatic_seconds",
                )
            },
            "false_active_seconds": member_metrics["false_active_seconds"],
            "occupied_seconds": member_metrics["occupied_seconds"],
            "empty_seconds": member_metrics["empty_seconds"],
        }
    return {
        "scorer_version": 3,
        "start": problem.start,
        "end": problem.end,
        "room": _assessment(metrics, bundle, mode),
        "devices": members,
        "excluded": {
            "no_shared_recording_seconds": max(
                0, problem.end - problem.start - metrics["covered_seconds"]
            ),
            "unlabelled_seconds": max(
                0,
                metrics["covered_seconds"] - metrics["occupied_seconds"] - metrics["empty_seconds"],
            ),
        },
        "shared_recording_seconds": metrics["covered_seconds"],
        "timing": {k: v.timing for k, v in problem.radars.items()},
        "sample_basis": "observed_seconds / 6",
        "label_basis": "Human precedence; automatic evidence retains admission and provenance.",
    }


def _before_bundle(bundle):
    if "before" not in bundle:
        raise ValueError("Missing before evaluator measurements")
    return bundle["before"]


def room_report(
    problem: FitProblem, result: FitResult, skipped: dict[str, str], audit: dict | None
) -> dict:
    """Preserve per-zone assessments; linked sets never invent union-room truth."""
    mode = _mode(result)
    groups = {gid: {"skipped": reason} for gid, reason in skipped.items()}
    for zone in problem.zones:
        if zone.group_id not in skipped:
            groups[zone.group_id] = _zone_report(problem, result, zone, mode)
    scored = [g for g in groups.values() if "after" in g]
    if not scored:
        raise ValueError("No scorable zone")
    report = dict(
        method="human_priority_v12",
        thresholds={
            k: {HISTORY_KEYS[c]: v for c, v in t.items()} for k, t in result.thresholds.items()
        },
        before=scored[0]["before"] if len(problem.zones) == 1 else {"room": None},
        after=scored[0]["after"] if len(problem.zones) == 1 else {"room": None},
        groups=groups,
        cost_before=sum(g["cost_before"] for g in scored),
        cost_after=sum(g["cost_after"] for g in scored),
        status=_room_status(problem, result, skipped),
        validation_scope="whole_episode_chronological",
        warnings=_warnings(mode),
        **_selection(result),
    )
    _room_policy(report, result.diagnostics.get("automatic_evidence"))
    _anomaly(report, audit)
    return report


def _zone_report(problem, result, zone, mode):
    bundle = _bundle(result, zone.group_id)
    problem = replace(
        problem, radars={k: v for k, v in problem.radars.items() if k in zone.members}
    )
    before = result.before if len(problem.zones) == 1 else result.before["zones"][zone.group_id]
    after = result.after if len(problem.zones) == 1 else result.after["zones"][zone.group_id]
    return {
        "before": _room_view(problem, before, _before_bundle(bundle), mode),
        "after": _room_view(problem, after, bundle, mode),
        "cost_before": before["cost"],
        "cost_after": after["cost"],
    }


def _room_status(problem, result, skipped):
    if skipped:
        return "uncertain"
    if len(problem.zones) == 1:
        return _status(result.after)
    statuses = {_status(m) for m in result.after["zones"].values()}
    return next(s for s in ("insufficient", "unsafe", "tradeoff", "ok") if s in statuses)


def _room_policy(report, audit):
    if audit is not None:
        report["automatic_evidence"] = deepcopy(audit)
        report["validation_scope"] = (
            "later_explicit_episodes"
            if audit["selection"]["validated"]
            else "estimated_without_independent_validation"
        )
