"""Report the same bounded timing replay used by threshold search."""

from .metrics import _TemporalMetrics
from .timing import TimingReplay


def _hit_mask(rows, thresholds):
    return sum(
        1 << i
        for i, row in enumerate(rows)
        if any(row[1].get(key, -1) > value for key, value in thresholds.items())
    )


def observations(groups, thresholds, config=None, *, recent=False):
    """Return eligible rows and their combined, optionally held detection state."""
    present, absent = groups.get("present", []), groups.get("not_present", [])
    replay = TimingReplay(present, absent, config)
    hits = replay.project(_hit_mask(present, thresholds), _hit_mask(absent, thresholds))
    result = []
    for rows, detected, eligible in zip((present, absent), hits, replay.eligible, strict=True):
        start = int(len(rows) * 0.8) if recent else 0
        result.extend(
            (row, bool(detected & (1 << i)))
            for i, row in enumerate(rows)
            if i >= start and eligible & (1 << i)
        )
    return result


def evaluate(groups, thresholds, config=None, *, recent=False):
    selected = observations(groups, thresholds, config, recent=recent)
    result = _measure(selected)
    if config and config.get("timeout") is not None:
        total = sum(
            len(rows[int(len(rows) * 0.8) :] if recent else rows) for rows in groups.values()
        )
        result["timing_warmup_samples"] = total - len(selected)
        result["basis"] = "sampled_timing_estimate"
    return result


def _measure(observations):
    counts = {"present": 0, "not_present": 0}
    hits = dict(counts)
    temporal = _TemporalMetrics()
    for row, hit in sorted(observations, key=lambda item: item[0][0]):
        ts, _values, label = row[:3]
        counts[label] += 1
        hits[label] += hit
        temporal.observe(ts, label, hit)
    return {
        **temporal.summary(),
        "present_samples": counts["present"],
        "not_present_samples": counts["not_present"],
        "false_negatives": counts["present"] - hits["present"],
        "false_positives": hits["not_present"],
        "sensitivity": hits["present"] / counts["present"] if counts["present"] else 0.0,
        "false_positive_rate": hits["not_present"] / counts["not_present"]
        if counts["not_present"]
        else 0.0,
    }


def timing_summary(config, groups):
    config = config or {}
    gaps = [
        b[0] - a[0]
        for rows in groups.values()
        for a, b in zip(rows, rows[1:], strict=False)
        if 0 < b[0] - a[0] <= 12
    ]
    return {
        "configuration": config,
        "active": config.get("timeout") is not None,
        "sample_interval_seconds": sorted(gaps)[len(gaps) // 2] if gaps else None,
        "scope": _scope(config),
        "model": "sampled_timing_estimate_v1",
    }


def _scope(config):
    if config.get("timeout") is None:
        return "raw"
    return (
        "reported_presence"
        if all(config.get(key) is not None for key in ("on_delay", "off_delay"))
        else "radar"
    )
