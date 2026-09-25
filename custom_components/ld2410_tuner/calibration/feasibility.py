"""Conservative proofs of conflicts between recorded human labels and targets.

A device triggers when any gate exceeds its threshold. Therefore each gate
individually must respect the device's false-sample budget. The most sensitive
settings satisfying these necessary bounds give an optimistic presence ceiling.
Failure at that ceiling proves a conflict; passing it does not prove feasibility.
"""

from math import floor

from .constants import MAX_FPR, MAX_MISSED_RUN, MIN_CLASS_SAMPLES, MIN_RECALL, SAMPLE_SECONDS
from .metrics import _masks, metrics


def assess_feasibility(positives, negatives, keys):
    """Explain provable human-data conflicts without changing the chosen thresholds."""
    if not positives or not negatives:
        return {"status": "not_assessed", "windows": []}
    windows = [_window_bounds(positives, negatives, keys, "all_human")]
    if min(len(positives), len(negatives)) >= MIN_CLASS_SAMPLES:
        windows.append(
            _window_bounds(
                positives[int(len(positives) * 0.8) :],
                negatives[int(len(negatives) * 0.8) :],
                keys,
                "recent_human",
            )
        )
    return {
        "status": "conflict" if any(window["conflict"] for window in windows) else "not_ruled_out",
        "windows": windows,
    }


def _window_bounds(positives, negatives, keys, scope):
    miss_budget = floor(len(positives) * (1 - MIN_RECALL) + 1e-9)
    false_budget = floor(len(negatives) * MAX_FPR + 1e-9)
    masks = {key: _masks(negatives, key) for key in keys}
    bounds = {
        key: next(t for t, mask in enumerate(values) if mask.bit_count() <= false_budget)
        for key, values in masks.items()
    }
    optimistic = metrics(positives + negatives, bounds)
    impossible = [row for row in positives if not _detected(row, bounds)]
    costs = sorted(_minimum_false_cost(row, masks, len(negatives)) for row in positives)
    minimum_false = costs[len(positives) - miss_budget - 1]
    conflict = (
        len(impossible) > miss_budget
        or optimistic["missed_presence_episodes"] > 0
        or optimistic["longest_missed_run_samples"] > MAX_MISSED_RUN
    )
    periods = _conflict_periods(impossible) if conflict else []
    return {
        "scope": scope,
        "conflict": conflict,
        "present_samples": len(positives),
        "not_present_samples": len(negatives),
        "allowed_missed_samples": miss_budget,
        "allowed_false_samples": false_budget,
        "minimum_false_samples_for_recall": minimum_false
        if minimum_false <= len(negatives)
        else None,
        "unavoidable_missed_samples": len(impossible),
        "unavoidable_missed_episodes": optimistic["missed_presence_episodes"],
        "unavoidable_longest_missed_run": optimistic["longest_missed_run_samples"],
        "periods": periods[:6],
        "period_count": len(periods),
    }


def _detected(row, thresholds):
    return any(row[1].get(key, -1) > threshold for key, threshold in thresholds.items())


def _minimum_false_cost(row, masks, absent_count):
    # To detect this observation through a gate, its threshold must be strictly
    # below the observed value. The highest such setting has the fewest false hits.
    return min(
        (
            masks[key][value - 1].bit_count()
            for key, value in row[1].items()
            if key in masks and value > 0
        ),
        default=absent_count + 1,
    )


def _conflict_periods(rows):
    periods = []
    for timestamp, _values, _label in rows:
        if periods and timestamp - periods[-1]["end"] <= SAMPLE_SECONDS:
            periods[-1]["end"] = timestamp + SAMPLE_SECONDS
            periods[-1]["samples"] += 1
        else:
            periods.append({"start": timestamp, "end": timestamp + SAMPLE_SECONDS, "samples": 1})
    return periods


def exclusive_presence(positives, thresholds):
    """Count labelled presence observations that depend on exactly one selected gate."""
    values = {key: [] for key in thresholds}
    for row in positives:
        hits = [key for key, threshold in thresholds.items() if row[1].get(key, -1) > threshold]
        if len(hits) == 1:
            key = hits[0]
            values[key].append(row[1][key])
    return {
        key: {
            "exclusive_presence_samples": len(energies),
            "weakest_exclusive_presence_energy": min(energies) if energies else None,
        }
        for key, energies in values.items()
    }
