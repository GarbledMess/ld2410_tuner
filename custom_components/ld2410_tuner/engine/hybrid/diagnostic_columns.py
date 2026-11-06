"""Column arithmetic for raw diagnostics on contiguous physical rows of any duration.

Rows are walked as runs: each row starts where the last ended and none has a missing
channel or no duration. A run costs one label walk, a piece costs one review update, and
a decision change costs one period update. Row durations still reach every total one at a
time through C-level folds, in row order, so each reported float is the same value that
adding the rows one by one in Python would give.
"""

import re
import struct
import tempfile
from array import array
from bisect import bisect_left, bisect_right
from collections import deque
from functools import reduce
from itertools import compress, repeat
from math import ceil
from operator import add, le, ne, sub, truediv

from ...const import HISTORY_KEYS
from ...history import pace
from .replay import TABLES

_CLEAN = re.compile(rb"\x00+")
_WRONG = re.compile(rb"\x01+")
_MISSING = bytes([0] * 255 + [1])
_INVERT = bytes([1, 0]) + bytes(254)


def _flags(block, channels, thresholds):
    hits, missing = 0, 0
    named = []
    for c in channels:
        column = block.columns[c]
        flags = column.translate(TABLES[thresholds[c]])
        named.append((HISTORY_KEYS[c], flags))
        hits |= int.from_bytes(flags, "little")
        if 255 in column:
            missing |= int.from_bytes(column.translate(_MISSING), "little")
    return hits, missing, named


def _no_duration(seconds):
    if min(seconds) > 0:
        return 0
    return int.from_bytes(bytes(map(le, seconds, repeat(0))), "little")


def _split(unusable, breaks):
    """Usable row ranges, cut wherever a row does not start at the previous row's end."""
    for match in _CLEAN.finditer(unusable):
        first, stop = match.span()
        while (cut := breaks.find(1, first, stop - 1)) >= 0:
            yield first, cut + 1
            first = cut + 1
        yield first, stop


class Prepared:
    """One block's durations, decisions and runs, shared by every collector of the pass."""

    __slots__ = ("block", "named", "runs", "seconds", "wrong")

    def __init__(self, block, channels, thresholds):
        n = len(block.times)
        self.block = block
        self.seconds = list(map(sub, block.ends, block.times))
        hits, missing, self.named = _flags(block, channels, thresholds)
        hit = hits.to_bytes(n, "little")
        self.wrong = {"present": hit.translate(_INVERT), "not_present": hit}
        unusable = (missing | _no_duration(self.seconds)).to_bytes(n, "little")
        breaks = bytes(map(ne, block.times[1:], block.ends))
        self.runs = tuple(_split(unusable, breaks))

    def clipped(self, first, stop, a, b):
        """Each row's seconds inside ``[a, b)``, where only the outer rows can be cut."""
        seconds = self.seconds[first:stop]
        if stop - first == 1:
            seconds[0] = b - a
        else:
            seconds[0] = self.block.ends[first] - a
            seconds[-1] = b - self.block.times[stop - 1]
        return seconds

    def gates(self, first, stop, state):
        """Channels on the wrong side of their threshold in any of these rows."""
        target = 0 if state == "present" else 1
        names = [name for name, flags in self.named if flags.find(target, first, stop) >= 0]
        return sorted(names) if stop - first > 1 else names


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


def _add_column(hist, column, seconds):
    for value, mass in zip(column, seconds, strict=True):
        hist[value] += mass


class _Period:
    """A pending disagreement; it becomes a review row only if the review would keep it."""

    __slots__ = ("end", "errors", "identity", "observed", "span", "spans", "start")

    def __init__(self, identity, start, span):
        self.identity, self.start, self.span = identity, start, span
        self.end, self.errors, self.observed, self.spans = start, 0, 0, []


class _Reviewer:
    """Applies one label piece to the review's pending session and period."""

    def __init__(self, review, diagnostics):
        self.review, self.d = review, diagnostics
        self.period = None

    def piece(self, rows, first, stop, a, b, span, seconds):
        sixths = list(map(truediv, seconds, repeat(6)))
        wrong = rows.wrong[span.state]
        identity = (span.state, span.source, span.episode_id)
        self._session(rows, (first, stop), a, b, span, identity, seconds, sixths)
        at = first
        for match in _WRONG.finditer(wrong, first, stop):
            lo, hi = match.span()
            if lo > at:
                self.close_period()
            left = a if lo == first else rows.block.times[lo]
            pending = self._period(identity, left, span)
            pending.end = b if hi == stop else rows.block.ends[hi - 1]
            pending.errors = reduce(add, sixths[lo - first : hi - first], pending.errors)
            pending.observed = reduce(add, seconds[lo - first : hi - first], pending.observed)
            pending.spans.append((rows, lo, hi))
            at = hi
        if at < stop:
            self.close_period()

    def _session(self, rows, bounds, a, b, span, identity, seconds, sixths):
        review, pending = self.review, self.review.session
        first, stop = bounds
        gates = rows.gates(first, stop, span.state)
        if pending and pending[0] == identity and pending[1]["end"] == a:
            row = pending[1]
            row["gates"] = list(self.d._merged_gates(tuple(row["gates"]), tuple(gates)))
            row["end"] = b
        else:
            row = self.d._session(a, b, span, 0)
            row.update(samples=0, observed_span_seconds=0, gates=gates)
            review._store(pending)
            review.session = (identity, row)
        wrong = rows.wrong[span.state][first:stop]
        row["samples"] = reduce(add, sixths, row["samples"])
        row["errors"] = reduce(add, compress(sixths, wrong), row["errors"])
        row["observed_span_seconds"] = reduce(add, seconds, row["observed_span_seconds"])
        row["short_burst"] = row["observed_span_seconds"] < 6

    def _period(self, identity, start, span):
        pending = self.period
        if pending and pending.identity == identity and pending.end == start:
            return pending
        self.close_period()
        self.period = _Period(identity, start, span)
        return self.period

    def close_period(self):
        pending, self.period = self.period, None
        if pending is None:
            return
        review = self.review
        kept = review.periods[pending.span.state]
        if len(kept) < 24 or pending.errors >= kept[0][0]:
            review._store((pending.identity, self._period_row(pending)), True)
        else:
            review.serial += 1
            review.count += 1

    def _period_row(self, pending):
        row = self.d._session(pending.start, pending.end, pending.span, 0)
        gates = None
        for rows, lo, hi in pending.spans:
            found = rows.gates(lo, hi, pending.span.state)
            merged = () if gates is None else self.d._merged_gates(tuple(gates), tuple(found))
            gates = found if gates is None else list(merged)
        row.update(
            samples=pending.errors,
            errors=pending.errors,
            observed_span_seconds=pending.observed,
            short_burst=pending.observed < 6,
            gates=gates,
        )
        return row


class Collector:
    """One pass's chosen diagnostics; a report that reads less is spared computing the rest.

    ``automatic`` names the classes whose automatic histograms are wanted.
    """

    def __init__(self, channels, thresholds, diagnostics, **wanted):
        self.channels, self.thresholds, self.d = channels, thresholds, diagnostics
        states = diagnostics.STATES
        self.details = wanted.get("details", True)
        self.automatic = wanted.get("automatic", states)
        self.durations = Durations(states) if wanted.get("cutoffs", True) else None
        self.histograms = diagnostics._histograms(channels)
        self.samples = {s: _Samples() for s in wanted.get("samples", states)}
        self.review = diagnostics._Review()
        self.reviewer = _Reviewer(self.review, diagnostics)

    def block(self, rows, pieces):
        times, ends = rows.block.times, rows.block.ends
        for first, stop in rows.runs:
            for a, b, span in pieces.between(times[first], ends[stop - 1]):
                if b > a:
                    self.piece(rows, first, stop, a, b, span)

    def piece(self, rows, low, high, a, b, span):
        block = rows.block
        first = bisect_right(block.ends, a, low, high)
        stop = bisect_left(block.times, b, low, high)
        seconds = rows.clipped(first, stop, a, b)
        if self.durations is not None:
            self.durations.add(block, first, stop, a, span.state, seconds)
        if self.details:
            self.histogram(rows, first, stop, span, seconds)
            if span.state in self.samples:
                self.samples[span.state].add(block, a, b, span.source)
        self.reviewer.piece(rows, first, stop, a, b, span, seconds)

    def histogram(self, rows, first, stop, span, seconds):
        human = span.source == "human"
        if human or span.state in self.automatic:
            source = "human" if human else "automatic"
            columns = rows.block.columns
            for c in self.channels:
                hist = self.histograms[c][source][span.state]
                _add_column(hist, columns[c][first:stop], seconds)

    def automatic_block(self, rows, pieces, states):
        """Fill automatic histograms for ``states`` on a later pass over the same rows."""
        block = rows.block
        self.automatic = states
        for low, high in rows.runs:
            for a, b, span in pieces.between(block.times[low], block.ends[high - 1]):
                if b > a and span.state in states and span.source != "human":
                    first = bisect_right(block.ends, a, low, high)
                    stop = bisect_left(block.times, b, low, high)
                    self.histogram(rows, first, stop, span, rows.clipped(first, stop, a, b))

    def unobserved_human(self, labelled):
        """Classes with a human label but no observed time, whose readers fall back."""
        if not self.channels:
            return ()
        hist = self.histograms[self.channels[0]]["human"]
        return tuple(s for s in labelled if not any(hist[s]))

    def result(self):
        self.reviewer.close_period()
        found = {
            "samples": {s: v.slots(self.channels, self.d) for s, v in self.samples.items()},
            "sample_counts": {s: v.count for s, v in self.samples.items()},
            "histograms": self.histograms,
        }
        if self.durations is None:
            found["review"] = self.review.result()
            return found
        cutoffs, totals = self.durations.result()
        found["recent_cutoffs"] = cutoffs
        found["review"] = self.review.result()
        found["observed_seconds"] = totals
        return found

    def close(self):
        if self.durations is not None:
            self.durations.stream.close()


def _labels_in(problem, truth, diagnostics):
    return [
        (max(a, problem.start), min(b, problem.end), s)
        for a, b, s in diagnostics._labels(truth)
        if a < problem.end and b > problem.start
    ]


def _prepared(problem, device_id, pattern, labels):
    """The pass's blocks with their shared arithmetic, resting between them."""
    if not labels:
        return
    channels = problem.radars[device_id].channels
    for block in problem.rows[device_id].blocks(labels[0][0], labels[-1][1]):
        pace.rest()
        if block.times:
            yield Prepared(block, channels, pattern[device_id])


def _run(problem, device_id, pattern, labels, pairs):
    try:
        for rows in _prepared(problem, device_id, pattern, labels):
            for target, pieces in pairs:
                target.block(rows, pieces)
        return [target.result() for target, _ in pairs]
    finally:
        for target, _ in pairs:
            target.close()


def collect(problem, device_id, pattern, truth, diagnostics):
    """Every diagnostic of one radar and one truth."""
    channels = problem.radars[device_id].channels
    labels = _labels_in(problem, truth, diagnostics)
    collector = Collector(channels, pattern[device_id], diagnostics)
    pairs = [(collector, diagnostics._LabelPieces(labels))]
    return _run(problem, device_id, pattern, labels, pairs)[0]


def _fill_fallback(problem, device_id, pattern, labels, report, labelled):
    """Automatic histograms for classes whose human labels turned out to hold no rows."""
    states = report.unobserved_human(labelled)
    if not states:
        return
    pieces = report.d._LabelPieces(labels)
    for rows in _prepared(problem, device_id, pattern, labels):
        report.automatic_block(rows, pieces, states)


def collect_pair(problem, device_id, pattern, truth, diagnostics):
    """What a device report reads for all labels and for human labels, from one pass.

    The report reads histograms, present samples and the review, and it reads a class's
    automatic histograms only when that class has no human time. The human side's reader
    takes its recent cutoffs and periods. Nothing else is computed for them. Should a human
    label cover no rows at all, one more pass fills the automatic histograms it left unread.
    """
    channels = problem.radars[device_id].channels
    labels = _labels_in(problem, truth, diagnostics)
    thresholds = pattern[device_id]
    human = [piece for piece in labels if piece[2].source == "human"]
    labelled = {piece[2].state for piece in human}
    report = Collector(
        channels,
        thresholds,
        diagnostics,
        cutoffs=False,
        samples=("present",),
        automatic=tuple(s for s in diagnostics.STATES if s not in labelled),
    )
    review = Collector(channels, thresholds, diagnostics, details=False, samples=())
    pairs = [
        (report, diagnostics._LabelPieces(labels)),
        (review, diagnostics._LabelPieces(human)),
    ]
    found = _run(problem, device_id, pattern, labels, pairs)
    _fill_fallback(problem, device_id, pattern, labels, report, labelled)
    return found


_PIECE = struct.Struct("<dBI")


class Durations:
    """Totals per class, then a second walk over spilled rows to find the recent cutoffs."""

    def __init__(self, states):
        self.states = states
        self.totals = dict.fromkeys(states, 0.0)
        self.stream = tempfile.SpooledTemporaryFile(max_size=65536)

    def add(self, block, first, stop, a, state, seconds):
        self.totals[state] = reduce(add, seconds, self.totals[state])
        self.stream.write(_PIECE.pack(a, self.states.index(state), stop - first))
        self.stream.write(block.times[first:stop].tobytes())
        self.stream.write(array("d", seconds).tobytes())

    def result(self):
        self.stream.seek(0)
        remaining = {s: 0.8 * t for s, t in self.totals.items()}
        cutoffs = {}
        while raw := self.stream.read(_PIECE.size):
            a, index, n = _PIECE.unpack(raw)
            state = self.states[index]
            times, seconds = array("d"), array("d")
            times.frombytes(self.stream.read(8 * n))
            seconds.frombytes(self.stream.read(8 * n))
            if state not in cutoffs:
                left = reduce(sub, seconds, remaining[state])
                if left < 0:
                    cutoffs[state] = _crossing(remaining[state], a, times, seconds)
                remaining[state] = left
        return cutoffs, self.totals


def _crossing(remaining, a, times, seconds):
    """The time inside this piece where ``remaining`` runs out, subtracting row by row."""
    if remaining < seconds[0]:
        return a + remaining
    remaining -= seconds[0]
    for start, duration in zip(times[1:], seconds[1:], strict=True):
        if remaining < duration:
            return start + remaining
        remaining -= duration
    raise ValueError("Recent cutoff is outside this piece")
