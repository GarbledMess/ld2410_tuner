"""Exact physical-span threshold replay, independent of truth and package metadata."""

from dataclasses import dataclass
from math import inf, isfinite, isnan

from ..const import HISTORY_KEYS


@dataclass(frozen=True, slots=True)
class Replay:
    coverage: tuple
    active: tuple
    modes: tuple
    basis: str = "physical_span_replay"


def _duration(value):
    if not isinstance(value, (int, float)) or not isfinite(value) or value < 0:
        raise ValueError("Timing values must be finite and nonnegative")
    return value


def _timing(config):
    config = config or {}
    hold = config.get("timeout")
    if hold is None:
        return 0, 0, 0
    hold = _duration(hold)
    if config.get("on_delay") is None or config.get("off_delay") is None:
        return hold, 0, 0
    return hold, _duration(config["on_delay"]), _duration(config["off_delay"])


def _pattern(thresholds):
    result = []
    for key, value in thresholds.items():
        if key not in HISTORY_KEYS or not isinstance(value, (int, float)):
            raise ValueError("Threshold pattern must use standard gates and finite values")
        if not isfinite(value) or not 0 <= value <= 100:
            raise ValueError("Thresholds must be finite and between zero and one hundred")
        result.append((HISTORY_KEYS.index(key), value))
    return tuple(result)


def _valid_span(span, previous):
    if not _finite(span.start) or not _finite(span.end) or span.end < span.start:
        raise ValueError("Physical span endpoints must be finite and ordered")
    if span.start < previous:
        raise ValueError("Physical spans must be ordered and disjoint")
    if len(span.values) != len(HISTORY_KEYS):
        raise ValueError("Physical spans must have one value for each standard gate")
    if any(
        value is not None and (not _finite(value) or not 0 <= value <= 100) for value in span.values
    ):
        raise ValueError("Physical energies must be finite, zero to one hundred or missing")


def _validated(items):
    previous = -inf
    for span in items:
        _valid_span(span, previous)
        previous = span.end
        if span.end > span.start:
            yield span


def _append(intervals, start, end):
    if end <= start:
        return
    if intervals and start <= intervals[-1][1]:
        intervals[-1] = (intervals[-1][0], max(end, intervals[-1][1]))
    else:
        intervals.append((start, end))


def _usable(span, pattern):
    return all(span.values[index] is not None for index, _ in pattern)


def _sessions(items, pattern):
    low, high, hits = None, None, []
    for span in items:
        usable = _usable(span, pattern)
        if _new_session(span, usable, high):
            yield from _closed(low, high, hits)
            low, high, hits = None, None, []
        if not usable:
            continue
        low = _opened(low, span.start)
        high = span.end
        if any(span.values[index] > value for index, value in pattern):
            _append(hits, span.start, span.end)
    yield from _closed(low, high, hits)


def _closed(low, high, hits):
    if low is not None:
        yield low, high, hits


def _opened(low, timestamp):
    return timestamp if low is None else low


def _new_session(span, usable, high):
    return not usable or (high is not None and span.start != high)


def _held(hits, hold, end):
    result = []
    for low, high in hits:
        _append(result, low, min(end, high + hold))
    return result


def _filtered(hardware, on, off, end):
    result = []
    active_start, expires = None, None
    for low, high in hardware:
        if active_start is not None and low <= expires:
            expires = min(end, high + off)
            continue
        if active_start is not None:
            _append(result, active_start, expires)
        active_start = low + on if high - low >= on else None
        expires = min(end, high + off)
    if active_start is not None:
        _append(result, active_start, expires)
    return result


def _clip(intervals, start, end):
    return tuple(
        (max(start, low), min(end, high))
        for low, high in intervals
        if high > start and low < end and min(end, high) > max(start, low)
    )


def replay(spans, thresholds, timing=None, *, start=-inf, end=inf):
    """Replay the complete supplied physical history, then clip the requested window.

    Selected gates are all required for trustworthy coverage, even disabled gates
    whose threshold is100. Missing required readings and explicit coverage gaps
    reset hold and delay state. Unselected missing gates do not end coverage.
    Unknown hardware timeout ignores filters, matching the legacy timing contract.
    Unknown on/off metadata disables both filters while retaining a known hold.
    Callers must supply preceding physical history to preserve query warmup.
    """
    if isnan(start) or isnan(end) or end < start:
        raise ValueError("Replay window must be ordered and not NaN")
    pattern = _pattern(thresholds)
    hold, on, off = _timing(timing)
    coverage, active, modes = [], [], set()

    def observed():
        for span in _validated(spans):
            modes.add(span.mode)
            yield span

    for low, high, hits in _sessions(observed(), pattern):
        _append(coverage, low, high)
        for first, after in _filtered(_held(hits, hold, high), on, off, high):
            _append(active, first, after)
    return Replay(_clip(coverage, start, end), _clip(active, start, end), tuple(sorted(modes)))


def _finite(value):
    return isinstance(value, (int, float)) and isfinite(value)
