"""Offline sampled timing replay for threshold fitting, not a live presence entity.

Observed hit runs provide a presence estimate through the hardware hold. Empty
readings allow activity anywhere between neighbouring observations; sparse data
must not make an isolated positive look like a proven sub-second rejected pulse.
"""

import re
from array import array
from bisect import bisect_left, bisect_right
from functools import cached_property, lru_cache
from itertools import compress, islice
from operator import le, lt

from .constants import SAMPLE_SECONDS

RUN = re.compile("1+")


@lru_cache(maxsize=8)  # The same hits are read for each kind of span.
def hit_runs(bits):
    """``(first, stop)`` of each run of set bits in ``bits``: samples ``first`` to ``stop - 1``."""
    return [match.span() for match in RUN.finditer(bin(bits)[:1:-1])]


def _merge(intervals):
    merged = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return merged


def merged(starts, ends, *, sort=False, ordered=False):
    """``_merge`` of the spans ``(start, end)`` as ``(starts, ends)`` lists; with ``sort``, of
    the spans in order of start (and end), as ``union`` does. Spans in order whose ends are in
    order too need no walk: one starts afresh where every span before it ended earlier, which
    is where the sorted ends are below the sorted starts. Other spans are walked. ``ordered``
    says that the starts and the ends are each in order, so that need not be checked."""
    if len(starts) < 2:
        return list(starts), list(ends)
    if ordered or (
        all(map(le, starts, islice(starts, 1, None))) and all(map(le, ends, islice(ends, 1, None)))
    ):
        cut = list(map(lt, ends, islice(starts, 1, None)))
        return (
            [starts[0], *compress(islice(starts, 1, None), cut)],
            [*compress(ends, cut), ends[-1]],
        )
    spans = sorted(zip(starts, ends, strict=True)) if sort else zip(starts, ends, strict=True)
    return tuple(map(list, zip(*_merge(spans), strict=True)))


def sample_times(rows):
    """The times of ``rows``: ``(time, ...)`` tuples, or readings kept as columns."""
    times = getattr(rows, "times", None)
    return list(times) if times is not None else [row[0] for row in rows]


def _sessions(rows, barriers=()):
    start = 0
    times = sample_times(rows)
    for index in range(1, len(rows)):
        previous, current = times[index - 1], times[index]
        changed_label = bisect_right(barriers, current) != bisect_right(barriers, previous)
        if _separated(rows, index, previous, current, changed_label):
            yield start, rows[start:index]
            start = index
    if rows:
        yield start, rows[start:]


def _separated(rows, index, previous, current, changed_label):
    ends = getattr(rows, "ends", None)
    if ends is not None and (ends[index - 1] is not None or ends[index] is not None):
        if ends[index - 1] is None or ends[index] is None:
            return True
        if ends[index - 1] > current:
            raise ValueError("Physical event cells overlap")
        return ends[index - 1] != current
    return current - previous > SAMPLE_SECONDS * 2 or changed_label


def make_window(rows, hold, on_delay, off_delay):
    if getattr(rows, "ends", None) is not None and rows.ends[0] is not None:
        from .event_timing import EventWindow

        return EventWindow(rows, hold, on_delay, off_delay)
    return _Window(rows, hold, on_delay, off_delay)


class _Window:
    def __init__(self, rows, hold, on_delay, off_delay):
        self.times = sample_times(rows)
        self.hold, self.on, self.off = hold, on_delay, off_delay
        self.mask = (1 << len(rows)) - 1
        warmup = max(hold + off_delay, on_delay)
        first = bisect_left(self.times, self.times[0] + warmup)
        self.eligible = self.mask ^ ((1 << first) - 1)
        self.link = max(hold, hold + off_delay - on_delay) if on_delay <= hold else hold

    @property
    def start(self):
        return self.times[0] + max(self.hold + self.off, self.on)

    @property
    def end(self):
        return self.times[-1]

    @cached_property
    def in_order(self):
        """Whether the samples are in time order, as the spans' own order depends on."""
        return all(map(le, self.times, islice(self.times, 1, None)))

    @cached_property
    def _before(self):
        """The time of the sample before ``stop``, indexed by ``stop``."""
        return [self.times[0], *self.times]

    @cached_property
    def _low(self):
        """Where a hardware span starts for a run that begins at each sample."""
        times = self.times
        return array(
            "d", [times[max(0, i - 1)] + (0.000001 if i else 0) for i in range(len(times))]
        )

    @cached_property
    def _high_upper(self):
        """Where a hardware span ends for a run that ends at each sample, with the hold."""
        times, last = self.times, len(self.times) - 1
        return array(
            "d",
            [max(times[min(last, i + 1)] + self.hold, times[i] + 1e-6) for i in range(last + 1)],
        )

    @cached_property
    def _high_possible(self):
        times = self.times
        spare = 1e-6 if self.hold == 0 else 0
        return array("d", [times[i] + self.hold + spare for i in range(len(times))])

    def _lower(self, bits):
        """``(starts, ends)`` of the presence the radar holds: runs of hits within ``link``
        seconds of each other make one cluster, held from the first sample to the last."""
        runs = hit_runs(bits)
        if not runs:
            return [], []
        firsts, stops = zip(*runs, strict=True)
        times, before = self.times, self._before
        apart = [
            times[first] - before[stop] > self.link
            for first, stop in zip(firsts[1:], stops, strict=False)
        ]
        heads = [firsts[0], *compress(firsts[1:], apart)]
        tails = [*compress(stops, apart), stops[-1]]
        starts = [times[head] for head in heads]
        ends = [before[tail] + self.hold for tail in tails]
        keep = [end - start >= self.on for start, end in zip(starts, ends, strict=True)]
        spare = 1e-6 if self.hold == 0 else 0
        return merged(
            [start + self.on for start in compress(starts, keep)],
            [end + self.off + spare for end in compress(ends, keep)],
            ordered=self.in_order,
        )

    def _hardware(self, bits, high):
        runs = hit_runs(bits)
        if not runs:
            return [], []
        low = self._low
        starts, ends = merged(
            [low[first] for first, _ in runs],
            [high[stop - 1] for _, stop in runs],
            ordered=self.in_order,
        )
        keep = [end - start >= self.on for start, end in zip(starts, ends, strict=True)]
        return merged(
            [start + self.on for start in compress(starts, keep)],
            [end + self.off for end in compress(ends, keep)],
            ordered=self.in_order,
        )

    def _upper(self, bits):
        return self._hardware(bits, self._high_upper)

    def _possible_onset(self, bits):
        # A transition happened sometime after the preceding low observation,
        # not necessarily at the next high snapshot. Keep the same observed
        # tail: this uncertainty must not invent longer quiet-presence support.
        return self._hardware(bits, self._high_possible)

    def project(self, bits, upper, earlier_onset=False):
        starts, ends = self._presence_spans(bits, earlier_onset) if not upper else self._upper(bits)
        result = 0
        for start, end in zip(starts, ends, strict=True):
            first = bisect_left(self.times, start)
            # Zero hold with zero delays still detects the observed instant.
            last = bisect_left(self.times, end) if end > start else bisect_right(self.times, end)
            result |= ((1 << (last - first)) - 1) << first
        return result

    def _presence_spans(self, bits, earlier_onset):
        if earlier_onset and self.on > 0:
            return self._possible_onset(bits)
        return self._lower(bits)


class GroupTiming:
    def __init__(self, rows, config, *, upper=False, barriers=(), earlier_onset=False):
        self.upper = upper
        self.earlier_onset = earlier_onset
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
                (offset, make_window(part, hold, on, off))
                for offset, part in _sessions(rows, barriers)
            ]
            self.eligible = sum(window.eligible << offset for offset, window in self.windows)
        # Per-fit bounded cache, released when this fitting instance is discarded.
        self.project = lru_cache(maxsize=256)(self._project)

    def _project(self, bits):
        if not self.active:
            return bits
        return sum(
            window.project((bits >> offset) & window.mask, self.upper, self.earlier_onset) << offset
            for offset, window in self.windows
        )


class TimingReplay:
    def __init__(self, positives, negatives, config=None):
        self.positive = GroupTiming(
            positives, config or {}, barriers=[row[0] for row in negatives], earlier_onset=True
        )
        self.latest_onset = GroupTiming(
            positives, config or {}, barriers=[row[0] for row in negatives]
        )
        self.negative = GroupTiming(
            negatives, config or {}, upper=True, barriers=[row[0] for row in positives]
        )

    def project(self, detected, false):
        return self.positive.project(detected), self.negative.project(false)

    @property
    def eligible(self):
        return self.positive.eligible, self.negative.eligible
