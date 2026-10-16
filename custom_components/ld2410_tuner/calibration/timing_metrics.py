"""Report the same bounded timing replay used by threshold search."""

from functools import reduce
from operator import or_

from .duration import DurationReplay
from .metrics import above, measure_observations
from .timing import TimingReplay


def hit_mask(rows, thresholds):
    if hasattr(rows, "planes_of"):  # Readings kept as columns: compared a bit at a time.
        everything = (1 << len(rows)) - 1
        return reduce(
            or_,
            (
                above(rows.planes_of(key), value, everything)
                for key, value in thresholds.items()
                if key in rows.layout
            ),
            0,
        )
    return sum(
        1 << i
        for i, row in enumerate(rows)
        if any(row[1].get(key, -1) > value for key, value in thresholds.items())
    )


def observations(groups, thresholds, config=None, *, recent=False):
    """Return eligible rows and their combined, optionally held detection state."""
    present, absent = groups.get("present", []), groups.get("not_present", [])
    replay = TimingReplay(present, absent, config)
    hits = replay.project(hit_mask(present, thresholds), hit_mask(absent, thresholds))
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
    result = measure_observations(selected)
    present, absent = groups.get("present", []), groups.get("not_present", [])
    result["duration"] = DurationReplay(present, absent, config).summary(
        hit_mask(present, thresholds), hit_mask(absent, thresholds), recent
    )
    if config and config.get("timeout") is not None:
        total = sum(
            len(rows[int(len(rows) * 0.8) :] if recent else rows) for rows in groups.values()
        )
        result["timing_warmup_samples"] = total - len(selected)
        result["basis"] = "sampled_timing_estimate"
        result["onset_uncertainty"] = onset_uncertainty(groups, thresholds, config, recent=recent)
    return result


def onset_uncertainty(groups, thresholds, config, *, recent=False):
    present, absent = groups.get("present", []), groups.get("not_present", [])
    replay = TimingReplay(present, absent, config)
    raw = hit_mask(present, thresholds)
    possible, latest = replay.positive.project(raw), replay.latest_onset.project(raw)
    mask = replay.positive.eligible
    if recent:
        mask &= ~((1 << int(len(present) * 0.8)) - 1)
    return {
        "samples": (mask & possible & ~latest).bit_count(),
        "misses_if_latest_onset": (mask & ~latest).bit_count(),
        "misses_if_earliest_onset": (mask & ~possible).bit_count(),
    }
