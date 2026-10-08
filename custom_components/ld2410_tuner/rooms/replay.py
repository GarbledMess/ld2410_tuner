"""Replay each radar's own timing before combining its detection with other radars."""

from ..calibration.duration import DurationGroup, _clip
from ..calibration.timing_metrics import hit_mask
from .intervals import Timeline, intersect, union


def radar(rows, thresholds, timing, barriers=()):
    ordered = sorted(rows, key=lambda row: row[0])
    bits = hit_mask(ordered, thresholds)
    present = DurationGroup(ordered, timing, barriers=barriers)
    absent = DurationGroup(ordered, timing, absent=True, barriers=barriers)
    return from_bits(present, absent, bits)


def from_bits(present, absent, bits):
    coverage, lower, possible, upper = [], [], [], []
    for offset, window, _confidence in present.windows:
        start = window.times[0] + max(window.hold + window.off, window.on)
        end = window.times[-1]
        if end <= start:
            continue
        hits = (bits >> offset) & window.mask
        coverage.append((start, end))
        lower.extend(_clip(present._intervals(window, hits, True), start, end))
        possible.extend(_clip(present._intervals(window, hits, False), start, end))
        upper.extend(_clip(absent._intervals(window, hits, False), start, end))
    return {
        "coverage": union(coverage),
        "lower": union(lower),
        "possible": union(possible),
        "upper": union(upper),
    }


def combined(radars):
    coverage = None
    for item in radars.values():
        coverage = item["coverage"] if coverage is None else intersect(coverage, item["coverage"])
    return {
        "coverage": coverage or [],
        **{
            key: union(span for item in radars.values() for span in item[key])
            for key in ("lower", "possible", "upper")
        },
    }


def lookups(replayed):
    return {key: Timeline(spans) for key, spans in replayed.items()}
