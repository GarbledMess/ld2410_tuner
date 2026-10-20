"""Label-independent duration indexes over immutable physical day columns.

Each gate stores runs per energy bin and their cumulative observed seconds. A range
query bisects those runs instead of materialising every full-vector reading again.
Finished indexes share the day's cache lifetime; unchanged live snapshots share their
builder's index. Labels and query boundaries are always applied at read time.
"""

import re
import struct
from array import array
from bisect import bisect_left, bisect_right
from collections import OrderedDict
from itertools import accumulate, compress, groupby, pairwise
from math import ceil, isnan
from operator import mul, ne, sub
from threading import RLock

MAX_BYTES = 64 * 1024 * 1024
_cache = OrderedDict()
_lock = RLock()
_cached_bytes = 0


MISSING = 255  # The energy a missing reading's runs group under; they are dropped.
_NONZERO = bytes([0] + [1] * 255)


def _runs(column):
    """A gate's runs of one value as ``(starts, ends, energies)`` in rows (``MISSING`` for a
    missing reading). Byte columns are split without per-row Python work."""
    if isinstance(column, bytes):
        if not column:
            return [], [], []
        # Byte i of (column[1:] XOR column[:-1]) is non-zero where row i + 1 changes value.
        size = len(column) - 1
        changed = int.from_bytes(column[1:], "big") ^ int.from_bytes(column[:-1], "big")
        marks = changed.to_bytes(size, "big").translate(_NONZERO)
        changes = list(compress(range(1, len(column)), marks))
        starts = [0, *changes]
        return starts, [*changes, len(column)], list(map(column.__getitem__, starts))
    starts, ends, energies = [], [], []
    for match in re.finditer(rb"(.{4})\1*", column.tobytes(), re.DOTALL):
        value = struct.unpack("<f", match[1])[0]
        starts.append(match.start() // 4)
        ends.append(match.end() // 4)
        energies.append(MISSING if isnan(value) else ceil(value))
    return starts, ends, energies


def _valid_bytes(column):
    if isinstance(column, bytes):
        return column.translate(bytes(1 if value <= 100 else 0 for value in range(256)))
    return bytes(not isnan(value) for value in column)


class _Bin:
    def __init__(self):
        self.starts, self.ends, self.mass = array("I"), array("I"), array("d", [0])

    @classmethod
    def of(cls, starts, ends, prefix):
        """Runs ``starts``/``ends`` (in row order) with their cumulative held seconds."""
        found = cls()
        found.starts, found.ends = array("I", starts), array("I", ends)
        held = map(sub, map(prefix.__getitem__, found.ends), map(prefix.__getitem__, found.starts))
        found.mass = array("d", accumulate(held, initial=0))
        return found

    def at(self, index, prefix):
        position = bisect_right(self.ends, index)
        result = self.mass[position]
        if position < len(self.starts) and self.starts[position] < index:
            result += prefix[index] - prefix[self.starts[position]]
        return result


def _pieces(day, known):
    """Rows marked in ``known`` as ``(first, after)`` row ranges without a recording gap."""
    breaks = list(compress(range(1, len(day.times)), map(ne, day.times[1:], day.ends[:-1])))
    for match in re.finditer(rb"[^\x00]+", bytes(known)):
        first, after = match.span()
        inner = breaks[bisect_right(breaks, first) : bisect_left(breaks, after)]
        yield from pairwise((first, *inner, after))


def _coverage(day, known):
    """Contiguous time of the rows marked in ``known``; pieces that meet are one range."""
    low = high = None
    for first, after in _pieces(day, known):
        if day.times[first] != high:
            if low is not None:
                yield low, high
            low = day.times[first]
        high = day.ends[after - 1]
    if low is not None:
        yield low, high


def _known(day):
    """Rows where any gate has a reading."""
    known = 0
    for column in day.columns:
        known |= int.from_bytes(_valid_bytes(column), "little")
    return bytearray(known.to_bytes(len(day.times), "little"))


class Summary:
    def __init__(self, day):
        self.day = day
        durations = list(map(sub, day.ends, day.times))
        self.prefix = array("d", accumulate(durations, initial=0))
        self.known = _known(day)
        self.gates = [self._gate(column) for column in day.columns]
        self.coverage = array("d", accumulate(map(mul, self.known, durations), initial=0))
        self.coverage_starts, self.coverage_ends = array("d"), array("d")
        for start, end in _coverage(day, self.known):
            self.coverage_starts.append(start)
            self.coverage_ends.append(end)

    def _gate(self, column):
        """Per energy, its runs in row order (a stable sort by energy keeps them so)."""
        starts, ends, energies = _runs(column)
        bins = {}
        order = sorted(range(len(starts)), key=energies.__getitem__)
        for energy, group in groupby(order, key=energies.__getitem__):
            if energy != MISSING:
                index = list(group)
                bins[energy] = _Bin.of(
                    map(starts.__getitem__, index), map(ends.__getitem__, index), self.prefix
                )
        return bins

    def _range(self, start, end):
        first = bisect_right(self.day.ends, start)
        after = bisect_left(self.day.times, end)
        return first, after

    def _clipped(self, first, after, start, end, bins=None):
        """Discard the boundary rows' time outside the query, including partial holds."""
        low = max(0, start - self.day.times[first])
        high = max(0, self.day.ends[after - 1] - end)
        if bins is None:
            return low * self.known[first] + high * self.known[after - 1]
        return low, high

    def histograms(self, start, end):
        result = [[0.0] * 101 for _ in self.gates]
        first, after = self._range(start, end)
        if end <= start or first >= after:
            return result
        low, high = self._clipped(first, after, start, end, bins=True)
        for gate, bins in enumerate(self.gates):
            for energy, stream in bins.items():
                result[gate][energy] = stream.at(after, self.prefix) - stream.at(first, self.prefix)
            self._subtract(result[gate], self.day.columns[gate], first, low)
            self._subtract(result[gate], self.day.columns[gate], after - 1, high)
        return result

    @staticmethod
    def _subtract(histogram, column, index, seconds):
        value = column[index]
        if value != 255 and not isnan(value):
            energy = ceil(value)
            histogram[energy] = max(0.0, histogram[energy] - seconds)

    def observed(self, start, end):
        first, after = self._range(start, end)
        if end <= start or first >= after:
            return 0.0
        return self.coverage[after] - self.coverage[first] - self._clipped(first, after, start, end)

    def coverage_ranges(self, start, end):
        """Contiguous any-gate observations, clipped to a half-open query window."""
        first = bisect_right(self.coverage_ends, start)
        after = bisect_left(self.coverage_starts, end)
        for index in range(first, after):
            low, high = max(start, self.coverage_starts[index]), min(end, self.coverage_ends[index])
            if high > low:
                yield low, high

    def valid_ranges(self, start, end, gates):
        """Observed coverage of any selected gate, with original gaps and missing values."""
        gates = set(gates)
        if gates == set(range(len(self.gates))):
            yield from self.coverage_ranges(start, end)
            return
        known = 0
        for gate in gates:
            known |= int.from_bytes(_valid_bytes(self.day.columns[gate]), "little")
        observed = known.to_bytes(len(self.day.times), "little")
        for low, high in _coverage(self.day, observed):
            if high > start and low < end:
                yield max(start, low), min(end, high)

    @property
    def nbytes(self):
        # A cached summary strongly owns its physical day, including legacy
        # float columns. Those arrays survive eviction from the day-store LRU.
        arrays = [self.prefix, self.coverage, self.known, self.coverage_starts, self.coverage_ends]
        arrays.extend((self.day.times, self.day.ends, *self.day.columns))
        arrays.extend(
            part
            for bins in self.gates
            for stream in bins.values()
            for part in (stream.starts, stream.ends, stream.mass)
        )
        return sum(len(part) * getattr(part, "itemsize", 1) + 128 for part in arrays) + 256 * len(
            self.day.guesses
        )


def of_day(day):
    """Reuse only the exact immutable day or unchanged live-builder snapshot."""
    owner = getattr(day, "_summary_owner", day)
    mark = (
        getattr(day, "_summary_version", 0),
        len(day.times),
        day.ends[-1] if day.ends else day.start,
    )
    key = id(owner), mark
    with _lock:
        cached = _cache.get(key)
        if cached is not None:
            _cache.move_to_end(key)
            return cached
    return _remember(key, Summary(day))


def remember(day, summary):
    """Install an executor-loaded index for its exact immutable day snapshot."""
    owner = getattr(day, "_summary_owner", day)
    mark = (
        getattr(day, "_summary_version", 0),
        len(day.times),
        day.ends[-1] if day.ends else day.start,
    )
    return _remember((id(owner), mark), summary)


def _remember(key, summary):
    global _cached_bytes
    with _lock:
        previous = _cache.pop(key, None)
        _cached_bytes -= previous.nbytes if previous is not None else 0
        _cache[key] = summary
        _cached_bytes += summary.nbytes
        while _cached_bytes > MAX_BYTES and _cache:
            _cached_bytes -= _cache.popitem(last=False)[1].nbytes
    return summary


def of(view, device_id, date, key=None):
    """A physical index; label/room ``key`` revisions cannot affect its stored durations."""
    day = view._days.day(device_id, date, until=getattr(view, "_history_until", None))
    return of_day(day) if day is not None else None


def fully_labelled(view, device_id, reader, start, end):
    """Whether own labels cover every observed interval in the requested snapshot."""
    from math import inf

    start = max(start, getattr(view, "_physical_history_start", -inf))
    until = getattr(view, "_history_until", None)
    end = min(end, getattr(view, "_attribution_until", inf), inf if until is None else until)
    if end <= start:
        return False
    if _labelled_range(reader, start, end):
        return _has_coverage(view, device_id, start, end)
    return _physical_label_coverage(view, device_id, reader, start, end)


def _physical_label_coverage(view, device_id, reader, start, end):
    from . import daystore, spans

    observed = False
    for date in spans._store_days(view, device_id, start, end):
        low, high = daystore.bounds(date)
        low, high = max(start, low), min(end, high)
        if _labelled_range(reader, low, high):
            observed = observed or _has_coverage(view, device_id, low, high)
            continue
        covered = _indexed_label_coverage(of(view, device_id, date), reader, low, high)
        if covered is None:
            return False
        observed |= covered
    return observed


def _indexed_label_coverage(summary, reader, low, high):
    if summary is None:
        return None
    observed = False
    for start, end in summary.coverage_ranges(low, high):
        observed = True
        if not _labelled_range(reader, start, end):
            return None
    return observed


def _labelled_range(reader, start, end):
    first, after = bisect_right(reader.edges, start), bisect_left(reader.edges, end)
    return all(reader(t) is not None for t in (start, *reader.edges[first:after]))


def _has_coverage(view, device_id, start, end):
    from . import spans

    return any(
        spans._column_bounds(day, low, high) is not None
        for day, windows in spans._selected_days(view, device_id, start, end)
        for low, high in windows
    )
