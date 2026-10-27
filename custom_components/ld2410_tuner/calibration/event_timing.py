"""Exact interval geometry behind the bounded threshold-mask replay."""

from array import array
from bisect import bisect_left
from itertools import islice
from math import isfinite

from .event_replay import _filtered, _held
from .timing import sample_times


class EventWindow:
    def __init__(self, rows, hold, on_delay, off_delay):
        self.times = sample_times(rows)
        self.ends = array("d", rows.ends) if isinstance(rows.ends, array) else list(rows.ends)
        _validate(self.times, self.ends)
        self.hold, self.on, self.off = hold, on_delay, off_delay
        self.start, self.end = self.times[0], self.ends[-1]
        self.mask = (1 << len(rows)) - 1
        self.eligible = self.mask
        self.in_order = True

    def raw(self, bits):
        from .timing import hit_runs

        runs = hit_runs(bits)
        return [self.times[first] for first, _ in runs], [self.ends[stop - 1] for _, stop in runs]

    def _lower(self, bits):
        starts, ends = self.raw(bits)
        hardware = _held(zip(starts, ends, strict=True), self.hold, self.end)
        active = _filtered(hardware, self.on, self.off, self.end)
        return [a for a, _ in active], [b for _, b in active]

    _upper = _lower
    _possible_onset = _lower

    def project(self, bits, upper=False, earlier_onset=False):
        starts, ends = self._lower(bits)
        result = 0
        for start, end in zip(starts, ends, strict=True):
            first, last = bisect_left(self.times, start), bisect_left(self.times, end)
            result |= ((1 << (last - first)) - 1) << first
        return result


def _validate(times, ends):
    if len(times) != len(ends) or not times:
        raise ValueError("Event replay needs one explicit endpoint per cell")
    if any(not isfinite(a) or not isfinite(b) or b <= a for a, b in zip(times, ends, strict=True)):
        raise ValueError("Event cells must have finite positive duration")
    if any(end != start for end, start in zip(ends, islice(times, 1, None), strict=False)):
        raise ValueError("One event window must have contiguous physical coverage")
