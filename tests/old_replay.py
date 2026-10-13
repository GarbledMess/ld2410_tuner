"""Reference: the room replay and the timing windows as they were before they were made fast.

Plain, slow and walked span by span. The efficiency tests check the fast paths against these on
random inputs; nothing else uses them."""

from bisect import bisect_left, bisect_right

import test_tuner  # noqa: F401  (registers tuner_under_test)
from tuner_under_test.calibration.duration import _clip
from tuner_under_test.rooms.intervals import union


def bit_runs(bits):
    while bits:
        start = (bits & -bits).bit_length() - 1
        shifted = bits >> start
        length = (shifted ^ (shifted + 1)).bit_length() - 1
        yield start, start + length - 1
        bits &= ~(((1 << length) - 1) << start)


def merge(intervals):
    result = []
    for start, end in intervals:
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(end, result[-1][1]))
        else:
            result.append((start, end))
    return result


def clusters(window, bits):
    found = []
    for start, end in bit_runs(bits):
        if found and window.times[start] - window.times[found[-1][1]] <= window.link:
            found[-1] = (found[-1][0], end)
        else:
            found.append((start, end))
    return found


def lower(window, bits):
    intervals = []
    for first, last in clusters(window, bits):
        start, end = window.times[first], window.times[last] + window.hold
        if end - start >= window.on:
            intervals.append(
                (start + window.on, end + window.off + (1e-6 if window.hold == 0 else 0))
            )
    return merge(intervals)


def upper(window, bits):
    times = window.times
    intervals = [
        (
            times[max(0, first - 1)] + (0.000001 if first else 0),
            max(times[min(len(times) - 1, last + 1)] + window.hold, times[last] + 1e-6),
        )
        for first, last in bit_runs(bits)
    ]
    hardware = merge(intervals)
    return merge([(a + window.on, b + window.off) for a, b in hardware if b - a >= window.on])


def possible(window, bits):
    times = window.times
    intervals = [
        (
            times[max(0, first - 1)] + (1e-6 if first else 0),
            times[last] + window.hold + (1e-6 if window.hold == 0 else 0),
        )
        for first, last in bit_runs(bits)
    ]
    hardware = merge(intervals)
    return merge([(a + window.on, b + window.off) for a, b in hardware if b - a >= window.on])


def raw(window, bits):
    times = window.times
    return [
        (
            (times[first - 1] + times[first]) / 2 if first else times[0],
            (times[last] + times[last + 1]) / 2 if last + 1 < len(times) else times[-1],
        )
        for first, last in bit_runs(bits)
    ]


def intervals(group, window, bits, latest):
    """``DurationGroup._intervals``."""
    if not group.timed:
        return raw(window, bits)
    if group.absent:
        return upper(window, bits)
    return lower(window, bits) if latest else possible(window, bits)


def project(window, bits, is_upper, earlier_onset=False):
    """``_Window.project``."""
    if is_upper:
        spans = upper(window, bits)
    elif earlier_onset and window.on > 0:
        spans = possible(window, bits)
    else:
        spans = lower(window, bits)
    result = 0
    for start, end in spans:
        first = bisect_left(window.times, start)
        last = bisect_left(window.times, end) if end > start else bisect_right(window.times, end)
        result |= ((1 << (last - first)) - 1) << first
    return result


def from_bits(present, absent, bits):
    """``replay.from_bits``: every window, all four span lists, one final union."""
    coverage, lower_spans, possible_spans, upper_spans = [], [], [], []
    for offset, window, _confidence in present.windows:
        start = window.times[0] + max(window.hold + window.off, window.on)
        end = window.times[-1]
        if end <= start:
            continue
        hits = (bits >> offset) & window.mask
        coverage.append((start, end))
        lower_spans.extend(_clip(intervals(present, window, hits, True), start, end))
        possible_spans.extend(_clip(intervals(present, window, hits, False), start, end))
        upper_spans.extend(_clip(intervals(absent, window, hits, False), start, end))
    return {
        "coverage": union(coverage),
        "lower": union(lower_spans),
        "possible": union(possible_spans),
        "upper": union(upper_spans),
    }


def rates(item, thresholds):
    """``Objective.rates``: replay every radar in full, unite, sum ``between`` span by span."""
    projected = [
        from_bits(radar.positive, radar.negative, radar.bits(thresholds[key]))
        for key, radar in item.radars.items()
    ]
    detected = union(span for part in projected for span in part["lower"])
    false = union(span for part in projected for span in part["upper"])
    result = {}
    for source in ("human", "automatic"):
        p, n = item.totals[source, "present"], item.totals[source, "not_present"]
        present = item.mass[source, "present"]
        absent = item.mass[source, "not_present"]
        result[source] = (
            1 - sum(present.between(a, b) for a, b in detected) / p if p else 0,
            sum(absent.between(a, b) for a, b in false) / n if n else 0,
        )
    return result
