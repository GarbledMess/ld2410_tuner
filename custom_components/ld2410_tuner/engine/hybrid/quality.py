"""Streaming quality sufficient statistics, independent of candidate search.

An episode contributes its normalized density once. Summing those densities as
each episode closes gives exactly the existing mean-density formula without
retaining an episode by gate matrix of 101-bin histograms.
"""

import re
from array import array
from bisect import bisect_left
from dataclasses import dataclass, replace
from functools import lru_cache
from hashlib import sha256
from operator import eq, lt

from ...calibration import event_quality
from ...calibration.quality import QualityModel, learn
from ...calibration.quality_evidence import QualityContext, _independent_reader
from ...const import HISTORY_KEYS
from ...history import guesses, pace, spans
from ...history.fit_rows import CapturedRowSource
from .types import EvidencePolicy, RowBlock

AUTOMATIC_HORIZON_DAYS = EvidencePolicy(0, False, {}).automatic_horizon_days


@dataclass(frozen=True)
class StreamQualityContext(QualityContext):
    density_counts: object = None
    histogram_slots: int = 0


class _Densities:
    def __init__(self, keys):
        self.keys, self.parent = tuple(keys), None
        self.positions = tuple(HISTORY_KEYS.index(key) for key in keys)
        self.current = [[0.0] * 101 for _ in keys]
        self.sums = {state: [[0.0] * 101 for _ in keys] for state in event_quality.LABELS}
        self.counts = {state: [0] * len(keys) for state in event_quality.LABELS}
        self.empty = [[0.0] * 101 for _ in keys]
        self.presence = [[0] * 101 for _ in keys]
        self.episodes = {}

    def add(self, cell):
        low, high, values, _state, parent = cell
        if parent is None:
            return
        if parent is not self.parent and parent != self.parent:
            self.finish()
            self.parent = parent
        self._values(values, high - low)

    def _values(self, values, seconds):
        observed = False
        for gate, position in enumerate(self.positions):
            energy = values[position]
            if energy is None:
                continue
            observed = True
            self.current[gate][energy] += seconds
            if self.parent.state == "not_present":
                self.empty[gate][energy] += seconds
        if observed:
            self.episodes[self.parent] = None

    def finish(self):
        if self.parent is None:
            return
        state = self.parent.state
        for gate, histogram in enumerate(self.current):
            total = sum(histogram)
            if total:
                self.counts[state][gate] += 1
                self._merge(gate, histogram, total, state)
            histogram[:] = [0.0] * 101

    def _merge(self, gate, histogram, total, state):
        for energy, seconds in enumerate(histogram):
            self.sums[state][gate][energy] += seconds / total
            if state == "present" and seconds:
                self.presence[gate][energy] += 1

    def densities(self, state):
        return {
            key: (tuple(value / max(1, self.counts[state][gate]) for value in histogram),)
            for gate, (key, histogram) in enumerate(zip(self.keys, self.sums[state], strict=True))
        }

    def masks(self):
        # _regime's >=2 support test can consume these two sufficient masks.
        return {
            key: (
                sum(1 << e for e, count in enumerate(hist) if count >= 1),
                sum(1 << e for e, count in enumerate(hist) if count >= 2),
            )
            for key, hist in zip(self.keys, self.presence, strict=True)
        }


_PHYSICAL_VALUES = (*range(255), None)
_MISSING = bytes(value == 255 for value in range(256))
_NOT = bytes(value == 0 for value in range(256))
_READABLE = re.compile(rb"\x01+")
_SEPARATED = re.compile(rb"\x00")


def _physical(source, start, end):
    start = max(start, getattr(source.view, "_physical_history_start", float("-inf")))
    for block in source.blocks(start, end):
        pace.rest()
        values = zip(
            *(map(_PHYSICAL_VALUES.__getitem__, column) for column in block.columns), strict=True
        )
        for lo, hi, row in zip(block.times, block.ends, values, strict=True):
            yield spans.Span(lo, hi, row, "event")


def _native_intervals(source, reader, owner, start, end, densities, digest):
    """Usable label intervals over captured columns, in chronological order.

    Only rows inside an explicit label are visited one at a time (their densities need
    them); every other row is geometry, read from the columns as whole ranges.
    """
    labels = event_quality._LabelCells(reader, owner, start, end)
    vector = _vector_reader(source.channels)
    low = max(start, getattr(source.view, "_physical_history_start", float("-inf")))
    for block in source.blocks(low, end):
        pace.rest()
        digest.update(block.times.tobytes() + block.ends.tobytes())
        digest.update(b"".join(block.columns[c] for c in source.channels))
        readable = _readable(block, source.channels)
        for piece in _label_pieces(block, labels, start, end):
            digest.update(repr((piece[2], piece[3])).encode())
            if piece[3] is not None:
                _add_densities(densities, block, piece, source.channels, vector)
            yield from _piece_intervals(block, piece, readable)


def _readable(block, channels):
    """One byte per row: 1 when any selected gate has a reading."""
    missing = -1
    for c in channels:
        missing &= int.from_bytes(block.columns[c].translate(_MISSING), "little")
    n = len(block.times)
    if not channels:
        return bytes(n)
    return missing.to_bytes(n, "little").translate(_NOT)


def _label_pieces(block, labels, start, end):
    """``(first, stop, state, parent, low, high)``: rows under one label, whole or clipped."""
    times, ends = block.times, block.ends
    at, n = 0, len(times)
    whole = all(map(lt, times, ends))  # An empty or reversed row has no duration to share.
    cursor = start
    while at < n:
        low, high = max(cursor, times[at]), min(end, ends[at])
        if low >= high:
            at += 1
            continue
        state, parent, edge = labels.at(low)
        stop = bisect_left(ends, min(edge, end), at, n) if whole else at
        if stop > at and times[at] >= cursor:
            yield at, stop, state, parent, None, None
            at = stop
            continue
        cursor = min(high, edge)
        yield at, at + 1, state, parent, low, cursor
        at += cursor >= high


def _add_densities(densities, block, piece, channels, vector):
    first, stop, state, parent, low, high = piece
    part = _slice(block, first, stop)
    values = _block_vectors(part, channels, vector)
    if low is not None:
        densities.add((low, high, next(values), state, parent))
        return
    for lo, hi, row in zip(part.times, part.ends, values, strict=True):
        densities.add((lo, hi, row, state, parent))


def _piece_intervals(block, piece, readable):
    first, stop, state, parent, low, high = piece
    human = parent if state in event_quality.LABELS else state
    if low is not None:
        if readable[first]:
            yield low, high, (human, array("d", (low,)), array("d", (high,)))
        return
    times, ends = block.times, block.ends
    for match in _READABLE.finditer(readable, first, stop):
        a, b = match.span()
        for lo, hi in _joined_rows(times, ends, a, b):
            yield times[lo], ends[hi - 1], (human, times[lo:hi], ends[lo:hi])


def _joined_rows(times, ends, first, stop):
    """Ranges of rows each starting exactly where the row before it ended."""
    offset = first + 1
    joined = bytes(map(eq, times[offset:stop], ends[first : stop - 1]))
    for match in _SEPARATED.finditer(joined):
        cut = offset + match.start()
        yield first, cut
        first = cut
    yield first, stop


def _slice(block, first, stop):
    return RowBlock(
        block.times[first:stop],
        block.ends[first:stop],
        tuple(column[first:stop] for column in block.columns),
    )


def _vector_reader(channels):
    @lru_cache(maxsize=128)
    def read(values):
        row = [None] * len(HISTORY_KEYS)
        for channel, value in zip(channels, values, strict=True):
            row[channel] = _PHYSICAL_VALUES[value]
        return tuple(row)

    return read


def _block_vectors(block, channels, vector):
    if len(channels) <= 4:
        return map(vector, zip(*(block.columns[c] for c in channels), strict=True))
    return zip(
        *(map(_PHYSICAL_VALUES.__getitem__, column) for column in block.columns), strict=True
    )


def _runs(view, device_id, start, end, reader):
    clipped = [
        (max(start, run[0]), min(end, run[1]), *run[2:])
        for run in recorded_guesses(view, device_id, start, end).runs
        if run[0] < end and run[1] > start and run[1] > run[0]
    ]
    return event_quality._outside_unknown(clipped, reader)


def recorded_guesses(view, device_id, start, end):
    """Read captured runs directly; a different cutoff never shares a .res value."""
    from ...history import daystore

    days = getattr(view, "_days", None)
    if days is None:
        return guesses.of(view, device_id, start)
    found = []
    for date in days.days(device_id):
        low, high = daystore.bounds(date)
        if low < end and high > start:
            found.extend(days.guess_runs(device_id, date, until=end))
    return guesses.Guesses(guesses._merged(found))


def collect_quality(
    view,
    device_id,
    keys,
    start,
    end,
    human_reader,
    owner,
    *,
    automatic_horizon_days=AUTOMATIC_HORIZON_DAYS,
):
    """Rebuild independent training evidence through bounded captured row blocks."""
    end = min(end, getattr(view, "_attribution_until", end), getattr(view, "_history_until", end))
    reader = _independent_reader(view.data["devices"][device_id], human_reader)
    owner = (device_id,) if owner is None else tuple(owner)
    source = CapturedRowSource(view, device_id, tuple(HISTORY_KEYS.index(k) for k in keys))
    auto_start = (
        start
        if automatic_horizon_days is None
        else max(start, end - automatic_horizon_days * 86400)
    )
    runs = _runs(view, device_id, auto_start, end, reader)
    densities, digest = _Densities(keys), sha256()
    intervals = _native_intervals(source, reader, owner, start, end, densities, digest)
    automatic, overlaps = event_quality._interval_evidence(intervals, runs, owner, keys)
    densities.finish()
    digest = event_quality._fingerprint(
        (),
        runs,
        keys,
        owner,
        view.data["devices"][device_id].get("presence_sources"),
        digest=digest,
    )
    return StreamQualityContext(
        tuple(densities.episodes),
        automatic,
        overlaps,
        densities.masks(),
        {
            key: tuple(seconds / 6 for seconds in hist)
            for key, hist in zip(keys, densities.empty, strict=True)
        },
        digest,
        runs,
        start,
        end,
        densities.densities("present"),
        densities.densities("not_present"),
        densities.counts,
        5 * 101 * len(keys),
    )


def learn_quality(context):
    """Restore exact episode counts around the existing reliability/regime formulas."""
    if isinstance(context, QualityModel):
        return context
    model = learn(context)
    if not isinstance(context, StreamQualityContext):
        return model
    regimes = {}
    for gate, key in enumerate(context.gate_empty):
        regime = model.regimes[key]
        full = regime.basis == "empirical_empty_tail"
        regimes[key] = replace(
            regime,
            occupied_episodes=context.density_counts["present"][gate],
            occupied_density_episodes=context.density_counts["present"][gate] if full else 0,
            empty_density_episodes=context.density_counts["not_present"][gate] if full else 0,
        )
    return replace(model, regimes=regimes)
