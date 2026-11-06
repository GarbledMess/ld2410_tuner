"""Column arithmetic for raw diagnostics on regular six-second physical clocks.

Fractional label edges remain exact. Rows off that clock use the scalar collector;
changing readings do not prevent compact review of unchanged decisions.
"""

import re
import struct
import tempfile
from bisect import bisect_left, bisect_right
from collections import Counter, deque
from functools import lru_cache, reduce
from itertools import chain, repeat
from math import ceil
from operator import add, sub, truediv
from re import finditer

from ...const import HISTORY_KEYS
from ...history import pace
from .replay import TABLES
from .types import RowBlock

_RUN = re.compile(rb"\x01{2,}")
_MISSING = bytes([0] * 255 + [1])


def _on_clock(start, end, previous):
    return start % 6 == 0 and end - start == 6 and previous == start


def _regular_runs(block):
    """Rows on the joined six-second clock, as ranges; every other row is left for scalars."""
    joined = chain((block.times[0],), block.ends[:-1])
    flags = bytes(map(_on_clock, block.times, block.ends, joined))
    return [match.span() for match in _RUN.finditer(flags)]


def _slice(block, first, stop):
    return RowBlock(
        block.times[first:stop],
        block.ends[first:stop],
        tuple(column[first:stop] for column in block.columns),
    )


def _flags(block, channels, thresholds):
    hits, missing = 0, 0
    gates = {}
    for c in channels:
        column = block.columns[c]
        flags = column.translate(TABLES[thresholds[c]])
        gates[c] = flags
        hits |= int.from_bytes(flags, "little")
        missing |= int.from_bytes(column.translate(_MISSING), "little")
    n = len(block.times)
    return hits.to_bytes(n, "little"), missing.to_bytes(n, "little"), gates


def _seconds(block, first, stop, a, b):
    starts, ends = block.times[first:stop], block.ends[first:stop]
    starts[0], ends[-1] = a, b
    return list(map(sub, ends, starts))


def _review(review, block, first, stop, a, b, span, error, gates):
    before = (review.session, review.period)
    initial = [None if p is None else dict(p[1]) for p in before]
    review.add(a, b, span, (b - a) * error, gates)
    seconds = _seconds(block, first, stop, a, b)
    for previous, pending in zip(initial, (review.session, review.period), strict=True):
        if pending is not None:
            _correct_sums(previous, pending[1], seconds, error)


def _correct_sums(previous, row, seconds, error):
    continued = previous is not None and previous["start"] == row["start"]
    for key, values in (
        ("samples", map(truediv, seconds, repeat(6))),
        ("errors", map(truediv, seconds if error else repeat(0, len(seconds)), repeat(6))),
        ("observed_span_seconds", seconds),
    ):
        initial = previous[key] if continued else 0
        row[key] = reduce(add, values, initial)
    row["short_burst"] = row["observed_span_seconds"] < 6


class _Samples:
    def __init__(self):
        self.chunks, self.count, self.retained = deque(), 0, 0

    def add(self, block, a, b, source):
        first, stop = ceil(a / 6), ceil(b / 6)
        count = stop - first
        if count <= 0:
            return
        self.count += count
        first = max(first, stop - 5000)
        self.chunks.append((block, first, stop, source))
        self.retained += stop - first
        while self.retained - (self.chunks[0][2] - self.chunks[0][1]) >= 5000:
            _, lo, hi, _ = self.chunks.popleft()
            self.retained -= hi - lo

    def slots(self, channels, diagnostics):
        samples = deque(maxlen=5000)
        for block, first, stop, source in self.chunks:
            for k in range(first, stop):
                i = bisect_right(block.times, k * 6) - 1
                samples.append((k * 6, source, bytes(block.columns[c][i] for c in channels)))
        return diagnostics.SampleSlots(channels, samples)


class Collector:
    def __init__(self, channels, thresholds, diagnostics, details=True):
        self.channels, self.thresholds, self.d = channels, thresholds, diagnostics
        self.details = details
        self.durations = Durations(diagnostics.STATES)
        self.histograms = diagnostics._histograms(channels)
        self.samples = {s: _Samples() for s in diagnostics.STATES}
        self.review = diagnostics._Review()
        self.classify = lru_cache(maxsize=4096)(self._classify)

    def block(self, block, pieces):
        if not block.times:
            return
        at = 0
        for first, stop in _regular_runs(block):
            if at < first:
                self.scalar(_slice(block, at, first), pieces)
            self.regular(_slice(block, first, stop), pieces)
            at = stop
        if at < len(block.times):
            self.scalar(_slice(block, at, len(block.times)), pieces)

    def regular(self, block, pieces):
        hits, missing, gates = _flags(block, self.channels, self.thresholds)
        for match in finditer(b"\x00+", missing):
            low, high = block.times[match.start()], block.ends[match.end() - 1]
            for a, b, span in pieces.between(low, high):
                self.piece(block, a, b, span, hits, gates)

    def piece(self, block, a, b, span, hits, gates):
        if b <= a:
            return
        first, stop = bisect_right(block.ends, a), bisect_left(block.times, b)
        self.durations.add(a, b, span.state, _seconds(block, first, stop, a, b))
        if self.details:
            self.histogram(block, first, stop, a, b, span)
            self.samples[span.state].add(block, a, b, span.source)
        for match in finditer(b"\x00+|\x01+", hits[first:stop]):
            lo, hi = first + match.start(), first + match.end()
            left, right = max(a, block.times[lo]), min(b, block.ends[hi - 1])
            error = bool(match[0][0]) != (span.state == "present")
            names = self.gates(gates, lo, hi, span.state)
            _review(self.review, block, lo, hi, left, right, span, error, names)

    def gates(self, gates, first, stop, state):
        target = b"\x00" if state == "present" else b"\x01"
        names = [HISTORY_KEYS[c] for c in self.channels if gates[c].find(target, first, stop) >= 0]
        return sorted(names) if stop - first > 1 else names

    def histogram(self, block, first, stop, a, b, span):
        source = "human" if span.source == "human" else "automatic"
        for c in self.channels:
            hist = self.histograms[c][source][span.state]
            column = block.columns[c]
            if stop - first == 1:
                hist[column[first]] += b - a
            else:
                hist[column[first]] += block.ends[first] - a
                for value, count in Counter(column[first + 1 : stop - 1]).items():
                    hist[value] += 6 * count
                hist[column[stop - 1]] += b - block.times[stop - 1]

    def scalar(self, block, pieces):
        for a, b, span, values in self.d._block_pieces(block, self.channels, pieces):
            if b <= a:
                continue
            self.durations.add(a, b, span.state, [b - a])
            if self.details:
                self.d._add_histograms(self.histograms, self.channels, values, span, b - a)
                self.samples[span.state].add(block, a, b, span.source)
            error, gates = self.classify(values, span.state)
            self.review.add(a, b, span, (b - a) * error, gates)

    def _classify(self, values, state):
        hit = any(v > self.thresholds[c] for c, v in zip(self.channels, values, strict=True))
        return hit != (state == "present"), self.d._error_gates(
            self.channels, values, self.thresholds, state
        )

    def result(self):
        cutoffs, totals = self.durations.result()
        return {
            "samples": {s: v.slots(self.channels, self.d) for s, v in self.samples.items()},
            "sample_counts": {s: v.count for s, v in self.samples.items()},
            "histograms": self.histograms,
            "recent_cutoffs": cutoffs,
            "review": self.review.result(),
            "observed_seconds": totals,
        }


def collect(problem, device_id, pattern, truth, diagnostics, human=False):
    channels = problem.radars[device_id].channels
    collector = Collector(channels, pattern[device_id], diagnostics)
    labels = [
        (max(a, problem.start), min(b, problem.end), s)
        for a, b, s in diagnostics._labels(truth)
        if a < problem.end and b > problem.start
    ]
    pairs = [(collector, diagnostics._LabelPieces(labels))]
    if human:
        review = Collector(channels, pattern[device_id], diagnostics, details=False)
        pairs.append(
            (review, diagnostics._LabelPieces([p for p in labels if p[2].source == "human"]))
        )
    try:
        if labels:
            for block in problem.rows[device_id].blocks(labels[0][0], labels[-1][1]):
                pace.rest()
                for target, pieces in pairs:
                    target.block(block, pieces)
        results = [target.result() for target, _ in pairs]
        return results if human else results[0]
    finally:
        for target, _ in pairs:
            target.durations.stream.close()


_DURATION = struct.Struct("<ddBIdd")


class Durations:
    def __init__(self, states):
        self.states = states
        self.totals = dict.fromkeys(states, 0.0)
        self.stream = tempfile.SpooledTemporaryFile(max_size=65536)

    def add(self, a, b, state, seconds):
        self.totals[state] = reduce(add, seconds, self.totals[state])
        self.stream.write(
            _DURATION.pack(a, b, self.states.index(state), len(seconds), seconds[0], seconds[-1])
        )

    def result(self):
        self.stream.seek(0)
        remaining = {s: 0.8 * t for s, t in self.totals.items()}
        cutoffs = {}
        while raw := self.stream.read(_DURATION.size):
            a, b, index, n, first, last = _DURATION.unpack(raw)
            state = self.states[index]
            left, cutoff = _advance(remaining[state], a, b, n, first, last)
            if cutoff is not None and state not in cutoffs:
                cutoffs[state] = cutoff
            remaining[state] = left
        return cutoffs, self.totals


def _advance(remaining, a, b, n, first, last):
    if remaining < first:
        return 0, a + remaining
    remaining -= first
    if n == 1:
        return remaining, None
    interior = n - 2
    if remaining < 6 * interior:
        skipped = int(remaining // 6)
        rest = reduce(sub, repeat(6, skipped), remaining)
        return 0, a + first + skipped * 6 + rest
    remaining = reduce(sub, repeat(6, interior), remaining)
    if remaining < last:
        return 0, b - last + remaining
    return remaining - last, None
