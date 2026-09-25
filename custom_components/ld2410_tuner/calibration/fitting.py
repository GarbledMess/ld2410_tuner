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
from .constants import SAMPLE_SECONDS as SAMPLE_SECONDS
from .feasibility import assess_feasibility, exclusive_presence
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


def _human_failures(measured):
    present, absent = measured["present_samples"], measured["not_present_samples"]
    checks = [
        (
            present and measured["sensitivity"] < MIN_RECALL,
            f"{measured['false_negatives']} human-labelled presence samples missed",
        ),
        (
            present and measured["missed_presence_episodes"],
            f"{measured['missed_presence_episodes']} human-labelled presence episodes missed",
        ),
        (
            present and measured["longest_missed_run_samples"] > MAX_MISSED_RUN,
            f"{measured['longest_missed_run_samples']} consecutive human-labelled samples missed",
        ),
        (
            absent and measured["false_positive_rate"] > MAX_FPR,
            f"{measured['false_positives']} false triggers in {measured['not_present_samples']} human-labelled empty-room samples",
        ),
        (
            absent and measured["false_trigger_bursts_per_hour"] > MAX_FALSE_BURSTS_PER_HOUR,
            f"{measured['false_trigger_bursts_per_hour']:.1f} false-trigger bursts per observed empty-room hour",
        ),
    ]
    return [message for failed, message in checks if failed]


def fit_thresholds(rows, keys, current=None, automatic=()):
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
            "warnings": [
                f"Need {MIN_CLASS_SAMPLES} usable observations of each state, from human labels or confident estimates. Human: {counts}; automatic: {auto_counts}."
            ],
        }

    # Backtest only earlier observations. Later estimates may have seen the
    # human holdout, so they must not enter this evaluation's fitting step.
    held_out, validation, validation_exclusions = _backtest(
        raw_counts, raw_groups, raw_auto, keys, current
    )
    thresholds, mass = _search(groups["present"], groups["not_present"], auto_groups, keys, current)
    evidence["effective_weight"] = mass
    all_rows = groups["present"] + groups["not_present"]
    training = metrics(all_rows, thresholds)
    recent = (
        metrics(_recent_rows(groups), thresholds)
        if min(counts.values()) >= MIN_CLASS_SAMPLES
        else None
    )
    estimated = metrics([row[:3] for group in auto_groups.values() for row in group], thresholds)
    failures = _candidate_failures(training, recent, counts, estimated)
    status = "unsafe" if failures else "ok"
    basis = _evidence_basis(all_rows, evidence["used"])
    warnings = _learning_warnings(failures, basis, held_out)
    proposals = _build_proposals(
        thresholds, all_rows, groups, auto_groups, status, failures, basis, evidence
    )
    result = {
        "method": METHOD,
        "outlier_filter": exclusions,
        "raw_audit": metrics(raw_groups["present"] + raw_groups["not_present"], thresholds),
        "feasibility": assess_feasibility(groups["present"], groups["not_present"], keys),
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
            "false_positive_rate": MAX_FPR,
            "missed_presence_episodes": 0,
            "longest_missed_run_samples": MAX_MISSED_RUN,
            "false_trigger_bursts_per_hour": MAX_FALSE_BURSTS_PER_HOUR,
        },
    }
    _current_validation(result, held_out, current, keys, validation)
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


def _backtest(counts, groups, auto_groups, keys, current):
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
    backtest, _ = _search(training["present"], training["not_present"], inferred, keys, current)
    return metrics(validation, backtest), validation, excluded


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
    if not counts["present"] and estimated["present_samples"] and estimated["sensitivity"] == 0:
        failures.append("No estimated presence observations are detected by this candidate")
    if (
        not counts["not_present"]
        and estimated["not_present_samples"]
        and estimated["false_positive_rate"] == 1
    ):
        failures.append("This candidate triggers on every estimated empty-room observation")


def _enough_samples(keys, counts, automatic):
    return bool(keys) and all(
        counts[label] + automatic[label] >= MIN_CLASS_SAMPLES for label in counts
    )


def _candidate_failures(training, recent, counts, estimated):
    failures = _human_failures(training)
    if recent:
        failures.extend("Recent human labels: " + failure for failure in _human_failures(recent))
    _automatic_failures(counts, estimated, failures)
    return failures


def _current_validation(result, held_out, current, keys, validation):
    if held_out and current and all(key in current for key in keys):
        result["current_validation"] = metrics(validation, {key: current[key] for key in keys})


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
