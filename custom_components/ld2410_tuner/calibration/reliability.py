"""Identify rare, separated signal dips before any threshold calculation.

The detector uses distributions and temporal context, never candidate errors.
Empty-room spikes remain evidence to reject. Original recordings are untouched.
"""

from bisect import bisect_right
from itertools import groupby

from .constants import MIN_CLASS_SAMPLES, SAMPLE_SECONDS

MAX_DIP_SAMPLES = 6
CONTEXT_SAMPLES = 2
MAX_OUTLIER_FRACTION = 0.01
GAP_RATIO = 3


def fit_outlier_model(groups, keys):
    """Learn rare lower-tail clusters and independent presence support from human data."""
    limits, support = {}, {}
    for key in keys:
        present = sorted(row[1][key] for row in groups["present"] if key in row[1])
        noise = sorted(row[1][key] for row in groups["not_present"] if key in row[1])
        if min(len(present), len(noise)) < MIN_CLASS_SAMPLES:
            continue
        ceiling = max(noise)
        if present[len(present) // 4] > ceiling:
            support[key] = ceiling
        limit = _tail_limit(present, ceiling)
        if limit is not None:
            limits[key] = limit
    return {"limits": limits, "support": support}


def _tail_limit(values, noise_ceiling):
    distinct = sorted(set(values))
    gaps = [(right - left, left) for left, right in zip(distinct, distinct[1:], strict=False)]
    candidates = []
    for gap, left in gaps:
        count = bisect_right(values, left)
        if left > noise_ceiling or count > len(values) * MAX_OUTLIER_FRACTION:
            continue
        cluster = values[:count]
        spread = cluster[(count - 1) * 3 // 4] - cluster[(count - 1) // 4]
        # Compare separation to the low cluster's own spread. Distinct legitimate
        # locations may have large gaps too; they must not mask this rare cluster.
        # One energy unit is the recording resolution, not a threshold floor.
        if gap >= GAP_RATIO * max(1, spread):
            candidates.append((gap, left))
    return max(candidates)[1] if candidates else None


def _sample_state(row, model):
    values = row[1]
    available = [values[key] <= limit for key, limit in model["limits"].items() if key in values]
    if not available:
        return None
    supported = any(values.get(key, -1) > limit for key, limit in model["support"].items())
    return any(available) and not supported


def filter_groups(groups, model):
    """Apply a fixed detector consistently; return retained rows and exclusion details."""
    positives, negatives = groups["present"], groups["not_present"]
    weak = [_sample_state(row, model) for row in positives]
    periods, mask = [], 0
    for is_weak, indices in groupby(range(len(positives)), key=weak.__getitem__):
        run = list(indices)
        if is_weak is not True or not _supported_dip(run, positives, negatives, weak):
            continue
        mask |= sum(1 << index for index in run)
        periods.append(
            {
                "start": positives[run[0]][0],
                "end": positives[run[-1]][0] + SAMPLE_SECONDS,
                "samples": len(run),
            }
        )
    if mask.bit_count() > len(positives) * MAX_OUTLIER_FRACTION:
        mask, periods = 0, []
    retained = {
        "present": [row for index, row in enumerate(positives) if not mask & (1 << index)],
        "not_present": list(negatives),
    }
    return retained, _report(groups, retained, periods, model)


def _supported_dip(run, positives, negatives, weak):
    first, last = run[0], run[-1]
    if len(run) > MAX_DIP_SAMPLES or first < CONTEXT_SAMPLES:
        return False
    if last + CONTEXT_SAMPLES >= len(positives):
        return False
    context = list(range(first - CONTEXT_SAMPLES, first)) + list(
        range(last + 1, last + CONTEXT_SAMPLES + 1)
    )
    if any(weak[index] is not False for index in context):
        return False
    times = [row[0] for row in positives[first - CONTEXT_SAMPLES : last + CONTEXT_SAMPLES + 1]]
    if any(not 0 < b - a <= SAMPLE_SECONDS * 2 for a, b in zip(times, times[1:], strict=False)):
        return False
    if positives[last][0] - positives[first][0] > SAMPLE_SECONDS * (MAX_DIP_SAMPLES - 1):
        return False
    return not any(times[0] <= row[0] <= times[-1] for row in negatives)


def _report(groups, retained, periods, model):
    return {
        "excluded": {label: len(groups[label]) - len(rows) for label, rows in retained.items()},
        "retained": {label: len(rows) for label, rows in retained.items()},
        "periods": periods[:12],
        "period_count": len(periods),
        "gate_limits": model["limits"],
    }


def prepare_evidence(groups, automatic, keys, models=None):
    """Build one filtered evidence set for search, metrics, feasibility and Apply."""
    if models is None:
        models = {
            "human": fit_outlier_model(groups, keys),
            "automatic": fit_outlier_model(automatic, keys),
        }
    human, human_report = filter_groups(groups, models["human"])
    inferred, auto_report = filter_groups(automatic, models["automatic"])
    report = {
        "method": "separated_temporal_dips_v2",
        "human": human_report,
        "automatic": auto_report,
    }
    return human, inferred, report, models
