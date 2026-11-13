"""Label-independent bounded replay with lossless decision folding and spilled records.

Rows that detect identically for every compared vector of one radar are folded
once (``always``/``possible`` crossings); only contested rows are decided per
vector, and identical outcome masks share one hit list. Timing runs once per unique
vector, and a zero-timing radar needs no state machine. Detection runs are read as
whole regular-expression matches, so constant stretches cost one interval, never
one step per row. Every fixed chunk's finalized coverage, raw hits, activity and
supported activity spill to a temporary file: scoring, and any later re-score with
other truth, never re-reads rows or re-runs timing. Chunk ends are fixed by the
label regions alone, so separately replayed vectors stay slice-aligned.
"""

import re
import struct
import sys
import tempfile
from array import array
from bisect import bisect_right
from hashlib import blake2b
from itertools import chain, compress, repeat
from operator import le, ne, sub

from ...calibration.event_replay import _timing
from ...const import HISTORY_KEYS
from ...history import pace
from .constants import CHUNK_SECONDS, MISSING, ROW_BLOCK_ROWS
from .scoring import Disjoint
from .timing import TimingStream

TABLES = tuple(bytes(1 if t < v != MISSING else 0 for v in range(256)) for t in range(101))
_BREAK = re.compile(rb"\x01")
_CUT = re.compile(rb"\x00")
_STEP = struct.Struct("<ddIII")
_COUNT = struct.Struct("<I")


def chunks(regions):
    """Chronological ``(lo, hi, last)`` pieces of at most ``CHUNK_SECONDS``."""
    for lo, hi in regions:
        at = lo
        while at < hi:
            end = min(hi, at + CHUNK_SECONDS)
            yield at, end, end >= hi
            at = end


def support_tables(radar, model):
    """Strict crossings that the quality model admits as positive support."""
    if model is None:
        return None
    tables = {}
    for c in radar.channels:
        admitted = bytes(
            v if v <= 100 and model.supports(HISTORY_KEYS[c], v) else 0 for v in range(256)
        )
        tables[c] = tuple(admitted.translate(table) for table in TABLES)
    return tables


def fold_masks(columns, vectors):
    """Exact byte-lane detection masks for every vector, folding constant rows.

    A row above every compared threshold on some channel always detects; one at
    or below every minimum never does. Only the remaining contested rows are
    decided per vector, and only on channels whose thresholds differ.
    """
    cache = {}
    always = possible = 0
    varying = []
    for c, column in columns.items():
        values = {vector[c] for vector in vectors}
        for t in {min(values), max(values)}:
            cache[c, t] = int.from_bytes(column.translate(TABLES[t]), "little")
        always |= cache[c, max(values)]
        possible |= cache[c, min(values)]
        if len(values) > 1:
            varying.append(c)
    contested = possible & ~always
    varying = _contested_channels(vectors, varying, cache, contested)
    _contested_masks(columns, vectors, varying, cache)
    masks = []
    for vector in vectors:
        mask = always
        for c in varying:
            mask |= cache[c, vector[c]] & contested
        masks.append(mask)
    rows = len(next(iter(columns.values()), b""))
    return masks, {"rows": rows, "contested": contested.bit_count(), "always": always.bit_count()}


def _contested_masks(columns, vectors, varying, cache):
    """Translate intermediate thresholds only after the global bounds differ."""
    for c in varying:
        for vector in vectors:
            key = c, vector[c]
            if key not in cache:
                cache[key] = int.from_bytes(columns[c].translate(TABLES[vector[c]]), "little")


def _contested_channels(vectors, varying, cache, contested):
    if not contested:
        return []
    return [c for c in varying if cache[c, min(v[c] for v in vectors)] & contested]


def _supported_masks(columns, vectors, tables, plain, unfiltered):
    """Each vector's supported detections; one whose support filters nothing keeps ``plain``."""
    cache = {}
    masks = []
    for i, vector in enumerate(vectors):
        if unfiltered[i]:
            masks.append(plain[i])
            continue
        mask = 0
        for c, column in columns.items():
            key = c, vector[c]
            if key not in cache:
                cache[key] = int.from_bytes(column.translate(tables[c][vector[c]]), "little")
            mask |= cache[key]
        masks.append(mask)
    return masks


def segments(times, ends):
    """Coverage segments and the row indexes where a physical gap starts one."""
    if not times:
        return [], []
    gaps = [m.start() + 1 for m in _BREAK.finditer(bytes(map(ne, times[1:], ends[:-1])))]
    result, first = [], 0
    for gap in gaps:
        result.append((times[first], ends[gap - 1]))
        first = gap
    result.append((times[first], ends[-1]))
    return result, gaps


def hits(mask, times, ends, gaps):
    """Detected runs as intervals, split at physical gaps; ``mask`` has one 0 or 1 byte a row.

    A run starts on a detecting row whose predecessor does not detect or lies across a gap,
    and stops on one whose successor does not or does. Finding those rows on the whole mask
    costs no Python step per run, and a real row shape has thousands of runs a chunk.
    """
    if not mask:
        return []
    before, after = mask << 8, mask >> 8
    if gaps:
        cut = int.from_bytes(bytes(_gap_flags(gaps, len(times))), "little")
        before &= ~cut
        after &= ~(cut >> 8)
    n = len(times)
    starts = compress(times, (mask & ~before).to_bytes(n, "little"))
    stops = compress(ends, (mask & ~after).to_bytes(n, "little"))
    return list(zip(starts, stops, strict=True))


def _gap_flags(gaps, n):
    flags = bytearray(n)
    for gap in gaps:
        flags[gap] = 1
    return flags


def _joinable(close, segment, previous):
    return 1 if close and segment == previous else 0


def premerge(hits, segments, hold):
    """Join hits the hold would merge anyway: same coverage segment, gap <= hold."""
    if len(hits) < 2 or hold <= 0:
        return hits
    xs = [h[0] for h in hits]
    ys = [h[1] for h in hits]
    starts = [a for a, _ in segments]
    later = map(bisect_right, repeat(starts), xs[1:])
    earlier = map(bisect_right, repeat(starts), ys[:-1])
    close = map(le, map(sub, xs[1:], ys[:-1]), repeat(hold))
    join = bytes(map(_joinable, close, later, earlier))
    out, first = [], 0
    for cut in _CUT.finditer(join):
        k = cut.start()
        out.append((xs[first], ys[k]))
        first = k + 1
    out.append((xs[first], ys[-1]))
    return out


def _extend(intervals, new):
    """Append ordered intervals, merging only an exact touch at the seam."""
    if new and intervals and intervals[-1][1] == new[0][0]:
        intervals[-1] = intervals[-1][0], new[0][1]
        intervals.extend(new[1:])
    else:
        intervals.extend(new)


class _BlockHits:
    """One block's detected runs, found once per distinct mask of all its streams.

    The row times are boxed once here: reading them from the arrays would box every row of
    the block again for each mask.
    """

    def __init__(self, times, ends, gaps, spans, hold):
        self.times, self.ends = times.tolist(), ends.tolist()
        self.gaps, self.spans, self.hold = gaps, spans, hold
        self.found = {}

    def of(self, mask):
        found = self.found.get(mask)
        if found is None:
            detected = hits(mask, self.times, self.ends, self.gaps)
            merged = premerge(detected, self.spans, self.hold)
            found = self.found[mask] = detected, merged, len(self.found)
        return found


def _shared(outputs, marks, kept):
    """Streams detecting alike in every block of a chunk hold one list."""
    for i, mark in enumerate(marks):
        outputs[i] = kept.setdefault(tuple(mark), outputs[i])


class _Radar:
    """Unique vectors of one radar and their timing states."""

    def __init__(self, radar, keys, tables):
        self.channels = tuple(sorted(radar.channels))
        self.keys = keys
        self.vectors = [dict(zip(self.channels, key, strict=True)) for key in keys]
        self.tables = tables
        self.hold, on, off = _timing(radar.timing)
        self.timed = bool(self.hold or on or off)
        self.physical = [TimingStream(radar.timing) for _ in keys] if self.timed else None
        self.support = [TimingStream(radar.timing) for _ in keys] if self.timed and tables else None
        self.unfiltered = [_filters_nothing(vector, tables) for vector in self.vectors]

    def start(self):
        self.coverage = []
        self.raw = [[] for _ in self.keys]
        self.raw_marks = [[] for _ in self.keys]
        self.positive = [[] for _ in self.keys] if self.tables else self.raw
        self.positive_marks = [[] for _ in self.keys] if self.tables else self.raw_marks

    def block(self, times, ends, columns):
        spans, gaps = segments(times, ends)
        _extend(self.coverage, spans)
        masks, _ = fold_masks(columns, self.vectors)
        block = _BlockHits(times, ends, gaps, spans, self.hold)
        self._feed(masks, block, spans, self.raw, self.raw_marks, self.physical)
        if self.tables:
            masks = _supported_masks(columns, self.vectors, self.tables, masks, self.unfiltered)
            self._feed(masks, block, spans, self.positive, self.positive_marks, self.support)

    def _feed(self, masks, block, spans, outputs, marks, streams):
        for i, mask in enumerate(masks):
            detected, merged, number = block.of(mask)
            _extend(outputs[i], detected)
            marks[i].append(number)
            if streams is not None:
                streams[i].feed(spans, merged)

    def finish(self, hi, last):
        """Drain every state to the fixed chunk end; activity is final before it."""
        kept = {}
        _shared(self.raw, self.raw_marks, kept)
        if self.positive is not self.raw:
            _shared(self.positive, self.positive_marks, kept)
        active = [_drain(s, hi, last) for s in self.physical] if self.timed else self.raw
        supported = active
        if self.support is not None:
            supported = [_drain(s, hi, last) for s in self.support]
        elif self.tables:
            supported = self.positive
        return self.coverage, self.raw, active, supported


def _filters_nothing(vector, tables):
    """Whether every crossing of ``vector`` is admitted, so support equals plain detection."""
    if not tables:
        return False
    return all(tables[c][t] == TABLES[t] for c, t in vector.items())


def _drain(stream, hi, last):
    if last:
        stream.close()
    stream.advance(hi)
    return stream.drain(hi)[1]


def _native(values):
    if sys.byteorder != "little":
        values.byteswap()
    return values


class Records:
    """One run's chronological chunk records, spilled to a temporary file.

    Each step stores a pool of distinct interval lists, then per radar its
    coverage and per stream its raw, active and supported list indexes.
    """

    def __init__(self, directory, rids, streams):
        self.rids, self.streams = tuple(rids), tuple(streams)
        self.file = tempfile.TemporaryFile(dir=directory, prefix="hybrid-replay-")
        self.steps = 0
        self._digests = [blake2b(digest_size=16) for _ in self.streams]

    def write(self, lo, hi, coverage, outputs):
        pool, identities = {}, {}

        def index(intervals):
            identity = id(intervals)
            if identity in identities:
                return identities[identity][1]
            raw = _native(array("d", chain.from_iterable(intervals))).tobytes()
            at = pool.setdefault(raw, len(pool))
            identities[identity] = intervals, at
            return at

        rid_index = array("I", (index(coverage[rid]) for rid in self.rids))
        stream_index = array("I", (index(v) for triple in outputs for v in triple))
        self.file.write(_STEP.pack(lo, hi, len(pool), len(rid_index), len(stream_index)))
        for raw in pool:
            self.file.write(_COUNT.pack(len(raw) // 16))
            self.file.write(raw)
        self._hash(pool, stream_index)
        self.file.write(_native(rid_index).tobytes() + _native(stream_index).tobytes())
        self.steps += 1

    def _hash(self, pool, stream_index):
        names = [blake2b(raw, digest_size=16).digest() for raw in pool]
        for at, digest in enumerate(self._digests):
            for list_index in stream_index[3 * at : 3 * at + 3]:
                digest.update(names[list_index])

    def signatures(self):
        """Per stream, what it recorded in every chunk: equal only if every list is equal."""
        return dict(zip(self.streams, (d.digest() for d in self._digests), strict=True))

    def rewind(self):
        self.file.flush()
        self.file.seek(0)

    def read(self, streams=None):
        """The next step: ``(lo, hi, coverage, outputs)``. With ``streams``, only those streams'
        outputs (and the coverage) are decoded: a scoring pass reads a few of every recorded
        stream. A list stored once is one object wherever it is read."""
        lo, hi, count, rids, stored = _STEP.unpack(self.file.read(_STEP.size))
        raws = [self._raw() for _ in range(count)]
        rid_index = _native(array("I", self.file.read(4 * rids)))
        stream_index = _native(array("I", self.file.read(4 * stored)))
        decoded = {}

        def at(i):
            found = decoded.get(i)
            if found is None:
                found = decoded[i] = _decoded(raws[i])
            return found

        coverage = {rid: at(i) for rid, i in zip(self.rids, rid_index, strict=True)}
        outputs = {}
        for k, key in enumerate(self.streams):
            if streams is None or key in streams:
                outputs[key] = tuple(at(i) for i in stream_index[3 * k : 3 * k + 3])
        return lo, hi, coverage, outputs

    def _raw(self):
        n = _COUNT.unpack(self.file.read(_COUNT.size))[0]
        return self.file.read(16 * n)

    @property
    def spilled_bytes(self):
        return self.file.tell() if not self.file.closed else 0

    def close(self):
        self.file.close()


def _decoded(raw):
    values = _native(array("d", raw))
    return Disjoint(zip(values[0::2], values[1::2], strict=True))


def replay(problem, providers, regions, wanted, models, directory):
    """Replay the ``wanted`` ``{rid: [vector keys]}`` and return spilled records."""
    radars = {
        rid: _Radar(problem.radars[rid], keys, models.get(rid))
        for rid, keys in sorted(wanted.items())
    }
    streams = [(rid, key) for rid, state in radars.items() for key in state.keys]
    records = Records(directory, radars, streams)
    operations = 0
    try:
        for lo, hi, last in chunks(regions):
            pace.rest()
            operations += _chunk(providers, radars, records, lo, hi, last)
    except BaseException:
        records.close()
        raise
    records.operations = operations
    return records


def _chunk(providers, radars, records, lo, hi, last):
    coverage, outputs, operations = {}, [], 0
    for rid, state in radars.items():
        times, ends, columns = providers[rid].window(lo, hi)
        state.start()
        for at in range(0, len(times), ROW_BLOCK_ROWS):
            stop = at + ROW_BLOCK_ROWS
            state.block(times[at:stop], ends[at:stop], {c: v[at:stop] for c, v in columns.items()})
        operations += len(times) * len(state.keys)
        cov, raw, active, supported = state.finish(hi, last)
        coverage[rid] = cov
        outputs.extend(zip(raw, active, supported, strict=True))
    records.write(lo, hi, coverage, outputs)
    return operations
