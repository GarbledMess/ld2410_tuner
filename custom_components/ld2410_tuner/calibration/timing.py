"""Offline sampled timing replay for threshold fitting, not a live presence entity.

Observed hit runs provide a presence estimate through the hardware hold. Empty
readings allow activity anywhere between neighbouring observations; sparse data
must not make an isolated positive look like a proven sub-second rejected pulse.
"""

from bisect import bisect_left, bisect_right
from functools import lru_cache

from .constants import SAMPLE_SECONDS


def bit_runs(bits):
    while bits:
        start = (bits & -bits).bit_length() - 1
        shifted = bits >> start
        length = (shifted ^ (shifted + 1)).bit_length() - 1
        yield start, start + length - 1
        bits &= ~(((1 << length) - 1) << start)


def _merge(intervals):
    merged = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def _sessions(rows, barriers=()):
    start = 0
    for index in range(1, len(rows)):
        previous, current = rows[index - 1][0], rows[index][0]
        changed_label = bisect_right(barriers, current) != bisect_right(barriers, previous)
        if current - previous > SAMPLE_SECONDS * 2 or changed_label:
            yield start, rows[start:index]
            start = index
    if rows:
        yield start, rows[start:]


class _Window:
    def __init__(self, rows, hold, on_delay, off_delay):
        self.times = [row[0] for row in rows]
        self.hold, self.on, self.off = hold, on_delay, off_delay
        self.mask = (1 << len(rows)) - 1
        warmup = max(hold + off_delay, on_delay)
        first = bisect_left(self.times, self.times[0] + warmup)
        self.eligible = self.mask ^ ((1 << first) - 1)
        self.link = max(hold, hold + off_delay - on_delay) if on_delay <= hold else hold

    def _clusters(self, bits):
        # Consecutive observed hits form one sampled run. Do not extend that
        # run to the next low reading; its tail starts at the last observed hit.
        clusters = []
        for start, end in bit_runs(bits):
            if clusters and self.times[start] - self.times[clusters[-1][1]] <= self.link:
                clusters[-1] = (clusters[-1][0], end)
            else:
                clusters.append((start, end))
        return clusters

    def _lower(self, bits):
        intervals = []
        for first, last in self._clusters(bits):
            start, end = self.times[first], self.times[last] + self.hold
            if end - start >= self.on:
                intervals.append(
                    (start + self.on, end + self.off + (1e-6 if self.hold == 0 else 0))
                )
        return _merge(intervals)

    def _upper(self, bits):
        intervals = [
            (
                self.times[max(0, first - 1)] + (0.000001 if first else 0),
                max(
                    self.times[min(len(self.times) - 1, last + 1)] + self.hold,
                    self.times[last] + 1e-6,
                ),
            )
            for first, last in bit_runs(bits)
        ]
        hardware = _merge(intervals)
        return _merge([(a + self.on, b + self.off) for a, b in hardware if b - a >= self.on])

    def project(self, bits, upper):
        intervals = self._upper(bits) if upper else self._lower(bits)
        result = 0
        for start, end in intervals:
            first = bisect_left(self.times, start)
            # Zero hold with zero delays still detects the observed instant.
            last = bisect_left(self.times, end) if end > start else bisect_right(self.times, end)
            result |= ((1 << (last - first)) - 1) << first
        return result


class GroupTiming:
    def __init__(self, rows, config, *, upper=False, barriers=()):
        self.upper = upper
        hold = config.get("timeout")
        self.active = hold is not None
        self.windows = []
        self.eligible = (1 << len(rows)) - 1
        if self.active:
            filters_known = (
                config.get("on_delay") is not None and config.get("off_delay") is not None
            )
            on, off = (config["on_delay"], config["off_delay"]) if filters_known else (0, 0)
            self.windows = [
                (offset, _Window(part, hold, on, off)) for offset, part in _sessions(rows, barriers)
            ]
            self.eligible = sum(window.eligible << offset for offset, window in self.windows)
        # Per-fit bounded cache, released when this fitting instance is discarded.
        self.project = lru_cache(maxsize=256)(self._project)

    def _project(self, bits):
        if not self.active:
            return bits
        return sum(
            window.project((bits >> offset) & window.mask, self.upper) << offset
            for offset, window in self.windows
        )


class TimingReplay:
    def __init__(self, positives, negatives, config=None):
        self.positive = GroupTiming(positives, config or {}, barriers=[row[0] for row in negatives])
        self.negative = GroupTiming(
            negatives, config or {}, upper=True, barriers=[row[0] for row in positives]
        )

    def project(self, detected, false):
        return self.positive.project(detected), self.negative.project(false)

    @property
    def eligible(self):
        return self.positive.eligible, self.negative.eligible
