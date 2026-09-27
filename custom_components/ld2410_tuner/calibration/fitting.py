"""Fit and validate thresholds against one consistently filtered evidence set."""

from __future__ import annotations

from math import isfinite

from .constants import AUTO_CLASS_CAP as AUTO_CLASS_CAP
from .constants import AUTO_WEIGHT as AUTO_WEIGHT
from .constants import MAX_CLASS_SAMPLES as MAX_CLASS_SAMPLES
from .constants import MAX_FALSE_BURSTS_PER_HOUR as MAX_FALSE_BURSTS_PER_HOUR
from .constants import MAX_FPR as MAX_FPR
from .constants import MAX_MISSED_RUN as MAX_MISSED_RUN
from .constants import METHOD as METHOD
from .constants import MIN_AUTO_CONFIDENCE as MIN_AUTO_CONFIDENCE
from .constants import MIN_CLASS_SAMPLES as MIN_CLASS_SAMPLES
from .constants import MIN_RECALL as MIN_RECALL
from .constants import MISSED_TIME_COST
from .constants import SAMPLE_SECONDS as SAMPLE_SECONDS
from .diagnostics import review_evidence
from .feasibility import exclusive_presence
from .metrics import _episode_masks as _episode_masks
from .metrics import _human_ranker as _human_ranker
from .metrics import _masks as _masks
from .metrics import _temporal_metrics as _temporal_metrics
from .metrics import _weight as _weight
from .metrics import _weighted_masks as _weighted_masks
from .metrics import metrics as metrics
from .reliability import filter_groups, prepare_evidence
from .search import _search
from .separation import gate_preference
from .timing_metrics import evaluate, timing_summary


def _human_failures(measured):
    duration = measured.get("duration")
    if duration is None:
        return _sample_failures(measured)
    failures = []
    recall = duration["presence_recall"]
    if recall is not None and recall < MIN_RECALL - 1e-12:
        failures.append(f"Estimated presence-time recall {recall:.3%}; target {MIN_RECALL:.1%}")
    if duration["missed_presence_episodes"]:
        failures.append(
            f"{duration['missed_presence_episodes']} occupied periods have no detection"
        )
    return failures


def _sample_failures(measured):
    # Legacy raw diagnostics and stored results retain their original meaning.
    failures = []
    if measured["present_samples"] and measured["sensitivity"] < MIN_RECALL:
        failures.append("Raw presence-sample recall target not met")
    for key in ("missed_presence_episodes", "longest_missed_run_samples"):
        limit = MAX_MISSED_RUN if key == "longest_missed_run_samples" else 0
        if measured.get(key, 0) > limit:
            failures.append(key)
    if measured["not_present_samples"] and measured["false_positive_rate"] > MAX_FPR:
        failures.append("Raw false-positive sample limit exceeded")
    if measured.get("false_trigger_bursts_per_hour", 0) > MAX_FALSE_BURSTS_PER_HOUR:
        failures.append("Raw false-trigger burst limit exceeded")
    return failures


def fit_thresholds(rows, keys, current=None, automatic=(), timing=None):
    """Fit all usable evidence; supported human observations take priority.

    Source proportions never block Apply. A chronological human backtest is
    reported separately, then the final recommendation is refit with all retained data.
    Its measurements are training evidence, not held-out accuracy guarantees.
    """
    keys = list(dict.fromkeys(keys))
    groups = {"present": [], "not_present": []}

    def clean(values):
        return {
            key: int(round(value))
            for key, value in values.items()
            if key in keys
            and isinstance(value, (int, float))
            and isfinite(value)
            and 0 <= value <= 100
        }

    groups, counts, auto_groups, auto_counts = _group_observations(rows, automatic, clean, groups)
    raw_groups, raw_auto, raw_counts = groups, auto_groups, counts
    groups, auto_groups, exclusions, _ = prepare_evidence(groups, auto_groups, keys)
    counts = {label: len(group) for label, group in groups.items()}
    auto_counts = {label: len(group) for label, group in auto_groups.items()}
    evidence = {
        "samples": auto_counts,
        "deferred_samples": 0,
        "base_weight": AUTO_WEIGHT,
        "class_weight_cap": AUTO_CLASS_CAP,
        "mean_confidence": {
            label: sum(row[3] for row in group) / len(group) if group else None
            for label, group in auto_groups.items()
        },
        "used": any(auto_counts.values()),
        "priority": "human_first",
    }
    if not _enough_samples(keys, counts, auto_counts):
        return {
            "status": "insufficient",
            "proposals": {},
            "counts": counts,
            "automatic_evidence": evidence,
            "outlier_filter": exclusions,
            "method": METHOD,
            "timing": timing_summary(timing, groups),
            "warnings": [
                f"Need {MIN_CLASS_SAMPLES} usable observations of each state, from human labels or confident estimates. Human: {counts}; automatic: {auto_counts}."
            ],
        }

    # Backtest only earlier observations. Later estimates may have seen the
    # human holdout, so they must not enter this evaluation's fitting step.
    held_out, validation, validation_exclusions = _backtest(
        raw_counts, raw_groups, raw_auto, keys, current, timing
    )
    thresholds, mass = _search(
        groups["present"], groups["not_present"], auto_groups, keys, current, timing
    )
    evidence["effective_weight"] = mass
    all_rows = groups["present"] + groups["not_present"]
    training = evaluate(groups, thresholds, timing)
    recent = (
        evaluate(groups, thresholds, timing, recent=True)
        if min(counts.values()) >= MIN_CLASS_SAMPLES
        else None
    )
    estimated = evaluate(auto_groups, thresholds, timing)
    failures = _candidate_failures(training, recent, counts, estimated)
    status = _outcome_status(failures, training, estimated)
    basis = _evidence_basis(all_rows, evidence["used"])
    warnings = _learning_warnings(failures, basis, held_out)
    if status == "uncertain":
        warnings.append(
            "Some observations have unresolved timing or no measurable duration. The presence-time target is not fully confirmed."
        )
    if status == "tradeoff":
        warnings.append(
            "Presence-time target met; false-positive time is reported as a penalty, not a failed learning job."
        )
    if status == "insufficient":
        warnings.append(
            "Too few observations remain after timing warm-up; record longer occupied and empty sessions."
        )
    proposals = _build_proposals(
        thresholds, all_rows, groups, auto_groups, status, failures, basis, evidence
    )
    result = {
        "method": METHOD,
        "outlier_filter": exclusions,
        "raw_audit": metrics(raw_groups["present"] + raw_groups["not_present"], thresholds),
        "raw_training": metrics(all_rows, thresholds),
        "timing": timing_summary(timing, groups),
        "review": review_evidence(groups, thresholds, timing),
        "feasibility": {
            "status": "not_assessed",
            "reason": "Sample-count feasibility does not establish duration-based feasibility",
            "windows": [],
        },
        "status": status,
        "evidence_basis": basis,
        "proposals": proposals,
        "warnings": warnings,
        "training": training,
        "validation": held_out,
        "validation_scope": "earlier_data_backtest",
        "validation_outliers": validation_exclusions,
        "recent_training": recent,
        "estimated_training": estimated,
        "counts": counts,
        "keys": keys,
        "automatic_evidence": evidence,
        "targets": {
            "sensitivity": MIN_RECALL,
            "scoring": "weighted_post_timing_error",
            "missed_time_cost": MISSED_TIME_COST,
            "missed_presence_episodes": 0,
            "false_positive_score": "negative_percent_of_observed_empty_time",
        },
    }
    _current_validation(result, held_out, current, keys, validation, timing)
    return result


def _group_observations(rows, automatic, clean, groups):
    manual_times = {row[0] for row in rows}
    for ts, values, label in sorted(rows, key=lambda row: row[0]):
        usable = clean(values)
        if label in groups and usable:
            groups[label].append((ts, usable, label))
    groups = {label: group[-MAX_CLASS_SAMPLES:] for label, group in groups.items()}
    counts = {label: len(group) for label, group in groups.items()}
    auto_groups = {"present": [], "not_present": []}
    for ts, values, label, confidence in sorted(automatic, key=lambda row: row[0]):
        usable = clean(values)
        if (
            label in auto_groups
            and MIN_AUTO_CONFIDENCE <= confidence <= 1
            and usable
            and ts not in manual_times
        ):
            auto_groups[label].append((ts, usable, label, confidence))
    auto_groups = {label: group[-MAX_CLASS_SAMPLES:] for label, group in auto_groups.items()}
    auto_counts = {label: len(group) for label, group in auto_groups.items()}
    return groups, counts, auto_groups, auto_counts


def _recent_rows(groups):
    return [row for group in groups.values() for row in group[int(len(group) * 0.8) :]]


def _backtest(counts, groups, auto_groups, keys, current, timing=None):
    if min(counts.values()) < MIN_CLASS_SAMPLES:
        return None, [], None
    earlier = {label: group[: int(len(group) * 0.8)] for label, group in groups.items()}
    later = {label: group[len(earlier[label]) :] for label, group in groups.items()}
    cutoff = min(row[0] for group in later.values() for row in group)
    earlier_auto = {
        label: [row for row in group if row[0] < cutoff] for label, group in auto_groups.items()
    }
    training, inferred, _, models = prepare_evidence(earlier, earlier_auto, keys)
    # Freeze the detector learned from training: holdout values cannot choose
    # outlier cutoffs or alter the fitted threshold configuration.
    retained, excluded = filter_groups(later, models["human"])
    validation = retained["present"] + retained["not_present"]
    backtest, _ = _search(
        training["present"], training["not_present"], inferred, keys, current, timing
    )
    return evaluate(retained, backtest, timing), validation, excluded


def _build_proposals(thresholds, all_rows, groups, auto_groups, status, failures, basis, evidence):
    proposals = {}
    exclusive = exclusive_presence(groups["present"], thresholds)
    for key, threshold in thresholds.items():
        proposals[key] = _gate_proposal(
            key, threshold, all_rows, groups, auto_groups, status, failures, basis, evidence
        )
        proposals[key].update(exclusive[key])
    return proposals


def _learning_warnings(failures, basis, held_out):
    warnings = list(failures)
    if basis != "human":
        warnings.append(
            "Inferred observations contribute with lower confidence; their proportion does not block Apply. Human-labelled results always take priority."
        )
    if held_out and _human_failures(held_out):
        warnings.append(
            "Earlier-data backtest missed its targets; the final recommendation was refit using all retained observations. Test it in new sessions."
        )
    warnings.append(
        "These are observed training results, not proof of field accuracy. Automatic confidence is heuristic; verify quiet presence and empty-room behaviour."
    )
    return warnings


def _automatic_failures(counts, estimated, failures):
    duration = estimated["duration"]
    recall = duration["presence_recall"]
    if not counts["present"] and recall is not None and recall < MIN_RECALL - 1e-12:
        failures.append(
            f"Estimated presence-time recall from automatic labels is {recall:.3%}; target {MIN_RECALL:.1%}"
        )
    if not counts["not_present"] and (duration["false_positive_percent"] or 0) >= 100 - 1e-9:
        failures.append("This candidate is active throughout automatically labelled empty time")


def _enough_samples(keys, counts, automatic):
    return bool(keys) and all(
        counts[label] + automatic[label] >= MIN_CLASS_SAMPLES for label in counts
    )


def _candidate_failures(training, recent, counts, estimated):
    failures = _human_failures(training)
    if (training["duration"]["false_positive_percent"] or 0) >= 100 - 1e-9:
        failures.append("This candidate is active throughout the observed empty-room time")
    if recent:
        failures.extend("Recent human labels: " + failure for failure in _human_failures(recent))
    _automatic_failures(counts, estimated, failures)
    return failures


def _current_validation(result, held_out, current, keys, validation, timing=None):
    if held_out and current and all(key in current for key in keys):
        groups = {
            label: [row for row in validation if row[2] == label]
            for label in ("present", "not_present")
        }
        result["current_validation"] = evaluate(groups, {key: current[key] for key in keys}, timing)


def _gate_proposal(
    key, threshold, all_rows, groups, auto_groups, status, failures, basis, evidence
):
    measured = metrics(all_rows, {key: threshold})
    manual_noise = [row[1][key] for row in groups["not_present"] if key in row[1]]
    noise = sorted(
        manual_noise or [row[1][key] for row in auto_groups["not_present"] if key in row[1]]
    )
    observed = any(
        key in row[1] for row in all_rows + auto_groups["present"] + auto_groups["not_present"]
    )
    return {
        **measured,
        "threshold": threshold,
        "status": status,
        "message": "; ".join(failures),
        "evidence_basis": basis,
        "specificity": 1 - measured["false_positive_rate"],
        "false_negative_rate": 1 - measured["sensitivity"],
        "noise_ceiling": max(noise) if noise else None,
        "noise_floor_p99": noise[int((len(noise) - 1) * 0.99)] if noise else None,
        "noise_source": "manual" if manual_noise else "automatic",
        "safety_margin": threshold - max(noise) if noise else None,
        "separation": gate_preference(key, groups, auto_groups, threshold),
        "auto_used": evidence["used"],
        "role": _gate_role(observed, threshold),
    }


def _evidence_basis(human_rows, automatic_used):
    if not human_rows:
        return "automatic"
    return "mixed" if automatic_used else "human"


def _gate_role(observed, threshold):
    if not observed:
        return "unchanged"
    return "suppressed" if threshold == 100 else "detection"


def _outcome_status(failures, training, estimated):
    if (
        min(training[key] + estimated[key] for key in ("present_samples", "not_present_samples"))
        < MIN_CLASS_SAMPLES
    ):
        return "insufficient"
    duration = training["duration"]
    auto = estimated["duration"]
    if not all(duration[key] + auto[key] > 0 for key in ("present_seconds", "empty_seconds")):
        return "insufficient"
    if failures:
        return "unsafe"
    if _time_uncertain(duration) or _time_uncertain(auto):
        return "uncertain"
    if any((m["false_positive_percent"] or 0) > 0 for m in (duration, auto)):
        return "tradeoff"
    return "ok"


def _time_uncertain(duration):
    recall, lower = duration["presence_recall"], duration["presence_recall_lower"]
    return bool(
        duration.get("unscored_presence_samples") or duration.get("unscored_empty_samples")
    ) or (recall is not None and lower is not None and recall - lower > 1e-9)
