"""Normalize legal patterns and transform the quiet-presence thresholds."""

from math import ceil, isfinite

from ...calibration.event_replay import _timing
from .constants import ATT_DEN, ATT_NUM, CHANNELS, EVIDENCE_POINTS, TOLERANCE
from .types import Pattern, RadarSpec


def _domain(values):
    if not values or any(type(t) is not int or not 0 <= t <= 100 for t in values):
        raise ValueError("Invalid integer domain")
    if tuple(sorted(set(values))) != values:
        raise ValueError("Integer domain must be sorted and unique")


def _channels(radar):
    if any(type(c) is not int or not 0 <= c < CHANNELS for c in radar.channels):
        raise ValueError("Unknown channel")
    if len(set(radar.channels)) != len(radar.channels):
        raise ValueError("Duplicate channel")
    if set(radar.channels) != set(radar.domains) or set(radar.channels) != set(radar.current):
        raise ValueError("Configured channels must match domain and current channels")


def _nearest(current, domain):
    _domain(domain)
    if type(current) not in (int, float) or not isfinite(current):
        raise ValueError("Threshold must be finite")
    return min(domain, key=lambda t: (abs(t - current), -t))


def normalize_pattern(radars: dict[str, RadarSpec]) -> Pattern:
    result = {}
    for rid, radar in radars.items():
        _channels(radar)
        _timing(radar.timing)
        result[rid] = {c: _nearest(radar.current[c], radar.domains[c]) for c in radar.channels}
    return result


def attenuated_threshold(threshold: int) -> int:
    return min(100, ceil((threshold + 1) * ATT_DEN / ATT_NUM) - 1)


def evidence_margin(occupied: int, empty: int) -> float:
    return TOLERANCE + EVIDENCE_POINTS / max(1, occupied + empty)
