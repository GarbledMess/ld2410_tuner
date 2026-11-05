"""Bounded physical diagnostics, independent of search and finalist replay.

These are raw per-gate/review diagnostics, never a substitute for timed evaluator
statistics. Recent cutoffs are found by observed class duration, not row count.
The facade must evaluate the fixed selected pattern on those class windows using
the same ScorePlan, and put those aggregates in FitResult.diagnostics.
"""

from array import array
from collections import deque
from collections.abc import Sequence
from functools import lru_cache
from heapq import heappush, heapreplace
from itertools import groupby, repeat
from math import ceil

from ...const import HISTORY_KEYS
from ...history import pace
from .evidence import _labels
from .types import FitProblem, Pattern, ScorePlan

STATES = ("present", "not_present")


def _pieces(problem, device_id, truth):
    labels = [
        (max(lo, problem.start), min(hi, problem.end), span)
        for lo, hi, span in _labels(truth)
        if lo < problem.end and hi > problem.start
    ]
    if not labels:
        return
    pieces = _LabelPieces(labels)
    channels = problem.radars[device_id].channels
    for block in problem.rows[device_id].blocks(labels[0][0], labels[-1][1]):
        pace.rest()
        yield from _block_pieces(block, channels, pieces)


def _block_pieces(block, channels, pieces):
    vectors = (
        zip(*(block.columns[c] for c in channels), strict=True)
        if channels
        else repeat((), len(block.times))
    )
    for i, (a, values) in enumerate(zip(block.times, vectors, strict=True)):
        if 255 not in values:
            for lo, hi, span in pieces.between(a, block.ends[i]):
                yield lo, hi, span, values


def _runs(problem, device_id, truth, compact=True):
    labels = [
        (max(lo, problem.start), min(hi, problem.end), span)
        for lo, hi, span in _labels(truth)
        if lo < problem.end and hi > problem.start
    ]
    if not labels:
        return
    if not compact or any(a % 6 or b % 6 for a, b, _ in labels):
        for piece in _pieces(problem, device_id, truth):
            yield (*piece, False)
        return
    pieces = _LabelPieces(labels)
    channels = problem.radars[device_id].channels
    for block in problem.rows[device_id].blocks(labels[0][0], labels[-1][1]):
        pace.rest()
        yield from _block_runs(block, channels, pieces)


def _block_runs(block, channels, pieces):
    vectors = (
        zip(*(block.columns[c] for c in channels), strict=True)
        if channels
        else repeat((), len(block.times))
    )
    for values, rows in groupby(enumerate(vectors), lambda item: item[1]):
        indexes = [i for i, _ in rows]
        if 255 not in values:
            yield from _run_pieces(block, indexes, values, pieces)


def _run_pieces(block, indexes, values, pieces):
    first, last = indexes[0], indexes[-1]
    contiguous = block.times[first] % 6 == 0 and all(
        block.ends[i] - block.times[i] == 6
        and block.times[i] == block.times[first] + (i - first) * 6
        for i in indexes
    )
    ranges = (
        [(block.times[first], block.ends[last])]
        if contiguous
        else [(block.times[i], block.ends[i]) for i in indexes]
    )
    for a, b in ranges:
        for lo, hi, span in pieces.between(a, b):
            yield lo, hi, span, values, contiguous and hi - lo > 6


class _LabelPieces:
    """Resolved labels walked once beside the chronological physical row stream."""

    def __init__(self, labels):
        self.labels = iter(labels)
        self.piece = next(self.labels, None)

    def between(self, lo, hi):
        while self.piece is not None and self.piece[1] <= lo:
            self.piece = next(self.labels, None)
        while self.piece is not None and self.piece[0] < hi:
            a, b, span = self.piece
            yield max(a, lo), min(b, hi), span
            if b > hi:
                break
            self.piece = next(self.labels, None)


def _valid_rows(block, channels):
    table = bytes([0] * 255 + [1])
    missing = 0
    for c in channels:
        missing |= int.from_bytes(block.columns[c].translate(table), "little")
    return missing.to_bytes(len(block.times), "little")


def _totals(problem, device_id, truth, regular=None):
    totals = dict.fromkeys(STATES, 0.0)
    labels = [
        (max(lo, problem.start), min(hi, problem.end), span)
        for lo, hi, span in _labels(truth)
        if lo < problem.end and hi > problem.start
    ]
    if not labels:
        return totals
    pieces = _LabelPieces(labels)
    channels = problem.radars[device_id].channels
    for block in problem.rows[device_id].blocks(labels[0][0], labels[-1][1]):
        pace.rest()
        _block_totals(totals, block, channels, pieces, regular)
    return totals


def _block_totals(totals, block, channels, pieces, regular):
    valid = _valid_rows(block, channels)
    for i, a in enumerate(block.times):
        if regular and (a % 6 or block.ends[i] - a != 6):
            regular[0] = False
        if not valid[i]:
            for lo, hi, span in pieces.between(a, block.ends[i]):
                totals[span.state] += hi - lo


def _keep(heap, value, serial, measure="samples"):
    item = (value[measure], -serial, value)
    if len(heap) < 24:
        heappush(heap, item)
    elif item[:2] > heap[0][:2]:
        heapreplace(heap, item)


def _histograms(channels):
    return {
        c: {source: {s: [0.0] * 101 for s in STATES} for source in ("human", "automatic")}
        for c in channels
    }


def _add_histograms(histograms, channels, values, span, seconds):
    source = "human" if span.source == "human" else "automatic"
    for c, v in zip(channels, values, strict=True):
        histograms[c][source][span.state][v] += seconds


def _session(a, b, span, errors):
    return {
        "start": a,
        "end": b,
        "state": span.state,
        "samples": (b - a) / 6,
        "errors": errors / 6,
        "observed_span_seconds": b - a,
        "duration_known": True,
        "short_burst": b - a < 6,
        "gates": [],
    }


@lru_cache(maxsize=4096)
def _merged_gates(left, right):
    return tuple(sorted(set(left) | set(right)))


def _append_session(pending, a, b, span, errors, gates):
    identity = (span.state, span.source, span.episode_id)
    if pending and pending[0] == identity and pending[1]["end"] == a:
        row = pending[1]
        row["gates"] = list(_merged_gates(tuple(row["gates"]), tuple(gates)))
        row["end"] = b
        row["samples"] += (b - a) / 6
        row["errors"] += errors / 6
        row["observed_span_seconds"] += b - a
        row["short_burst"] = row["observed_span_seconds"] < 6
        return pending, None
    row = _session(a, b, span, errors)
    row["gates"] = gates
    return (identity, row), pending


def _samples(samples, counts, a, b, span, channels, values):
    # Exactly one globally aligned clock, independent of source/row boundaries.
    first, stop = ceil(a / 6), ceil(b / 6)
    count = max(0, stop - first)
    counts[span.state] += count
    payload = bytes(values)
    for k in range(max(first, stop - 5000), stop):
        samples[span.state].append((k * 6, span.source, payload))


class SampleSlots(Sequence):
    """Compact immutable sample columns; mapping rows are decoded only on demand.

    At 18 channels the retained payload is 27 bytes per slot, instead of a Python
    energy dictionary per slot per radar. Output reports never serialize this
    internal diagnostic store; only per-gate summaries and top review rows escape.
    """

    def __init__(self, channels, samples):
        self.channels = channels
        self.times = array("d", (row[0] for row in samples))
        self.human = bytes(row[1] == "human" for row in samples)
        self.columns = tuple(bytes(row[2][i] for row in samples) for i in range(len(channels)))

    def __len__(self):
        return len(self.times)

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(len(self)))]
        return {
            "time": self.times[index],
            "source": "human" if self.human[index] else "automatic",
            "energies": {
                HISTORY_KEYS[c]: column[index]
                for c, column in zip(self.channels, self.columns, strict=True)
            },
        }

    def __eq__(self, other):
        if not isinstance(other, SampleSlots):
            return NotImplemented
        return (self.channels, self.times, self.human, self.columns) == (
            other.channels,
            other.times,
            other.human,
            other.columns,
        )


def _error_gates(channels, values, thresholds, state):
    return [
        HISTORY_KEYS[c]
        for c, v in zip(channels, values, strict=True)
        if (v > thresholds[c]) != (state == "present")
    ]


def _radar(problem, device_id, pattern, truth):
    channels = problem.radars[device_id].channels
    regular = [True]
    totals = _totals(problem, device_id, truth, regular)
    remaining = {s: 0.8 * totals[s] for s in STATES}
    cutoffs, histograms = {}, _histograms(channels)
    samples = {s: deque(maxlen=5000) for s in STATES}
    counts = dict.fromkeys(STATES, 0)
    review = _Review()

    @lru_cache(maxsize=4096)
    def classify(values, state):
        thresholds = pattern[device_id]
        hit = any(v > thresholds[c] for c, v in zip(channels, values, strict=True))
        return hit != (state == "present"), _error_gates(channels, values, thresholds, state)

    for a, b, span, values, merged in _runs(problem, device_id, truth, compact=regular[0]):
        if b <= a:
            continue
        state = span.state
        if state not in cutoffs and remaining[state] < b - a:
            cutoffs[state] = a + remaining[state]
        remaining[state] = max(0, remaining[state] - (b - a))
        _add_histograms(histograms, channels, values, span, b - a)
        _samples(samples, counts, a, b, span, channels, values)
        error, gates = classify(values, state)
        errors = (b - a) * error
        if merged:
            gates = list(_merged_gates(tuple(gates), tuple(gates)))
        review.add(a, b, span, errors, gates)
        if merged:
            _float_durations(review)
    return {
        "samples": {s: SampleSlots(channels, v) for s, v in samples.items()},
        "sample_counts": counts,
        "histograms": histograms,
        "recent_cutoffs": cutoffs,
        "review": review.result(),
        "observed_seconds": totals,
    }


def _float_durations(review):
    # A merged six-second run replaces additions of physical float durations.
    for pending in (review.session, review.period):
        if pending is not None:
            row = pending[1]
            row["observed_span_seconds"] = float(row["observed_span_seconds"])


class _Review:
    def __init__(self):
        self.sessions = []
        self.periods = {s: [] for s in STATES}
        self.session = self.period = None
        self.serial = self.count = 0

    def _store(self, pending, period=False):
        if pending is None:
            return
        row = pending[1]
        target = self.periods[row["state"]] if period else self.sessions
        _keep(target, row, self.serial, "errors")
        self.serial += 1
        self.count += period

    def add(self, a, b, span, errors, gates):
        self.session, finished = _append_session(self.session, a, b, span, errors, gates)
        self._store(finished)
        if errors:
            self.period, finished = _append_session(self.period, a, b, span, errors, gates)
            self._store(finished, True)
        else:
            self._store(self.period, True)
            self.period = None

    def result(self):
        self._store(self.session)
        self._store(self.period, True)
        per_class = [_ordered(self.periods[s]) for s in STATES]
        chosen = [row for rows in per_class for row in rows[:12]]
        overflow = [row for rows in per_class for row in rows[12:]]
        overflow.sort(key=lambda row: (-row["errors"], row["start"]))
        chosen.extend(overflow[: 24 - len(chosen)])
        chosen.sort(key=lambda row: (-row["errors"], row["start"]))
        return {
            "sessions": _ordered(self.sessions),
            "periods": chosen,
            "period_count": self.count,
            "influence": [],
            "excluded_automatically": False,
        }


def _ordered(heap):
    return [row for _, _, row in sorted(heap, key=lambda item: (-item[0], -item[1]))]


def diagnostics(problem: FitProblem, pattern: Pattern, plan: ScorePlan) -> dict:
    """Collect bounded raw diagnostics; never score or search thresholds.

    recent_cutoffs[state] identifies the newest 20% of that radar's observed
    labelled class time. Re-evaluate fixed thresholds after these cutoffs; do not
    infer recent error from the full-window aggregate. Histograms retain all
    observed duration; samples retain only the newest 5000 per class.
    """
    radars = {}
    for device_id in problem.radars:
        zones = [z for z in problem.zones if device_id in z.members]
        if len(zones) != 1:
            raise ValueError("Collect diagnostics on one zone at a time for shared radars")
        radars[device_id] = _radar(problem, device_id, pattern, zones[0].truth)
    return {
        "radars": radars,
        "evidence_budget": {
            "coverage": "all_observed_labelled_time",
            "scoring_mode": plan.mode,
            "row_count": plan.row_count,
            "finalist_limit": plan.finalist_limit,
            "automatic_horizon_days": problem.policy.automatic_horizon_days,
            "diagnostics": "newest_5000_per_class_raw_samples",
            "basis": "six_second_cells" if plan.mode == "cells" else "physical_interval_time",
        },
    }
