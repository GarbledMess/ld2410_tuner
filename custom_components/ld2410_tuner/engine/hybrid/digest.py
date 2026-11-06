"""Label-independent six-second maxima and spooled exact coverage geometry."""

import struct
import tempfile
from bisect import bisect_left, bisect_right
from collections.abc import Mapping
from dataclasses import dataclass
from itertools import repeat
from math import ceil, floor, isfinite
from operator import add, eq, ge, le, lt, mod, mul, sub
from re import finditer

from .constants import CELL_SECONDS, CHANNELS, MISSING
from .physical_index import PhysicalBuilder

_PAIR = struct.Struct("<dd")
_USABLE = bytes(0 if value == MISSING else 1 for value in range(256))
_ZERO_MISSING = bytes(range(255)) + b"\x00"


def cell_floor(t, resolution):
    """Find the grid index robustly at epoch magnitude."""
    k = floor(t / resolution)
    while k * resolution > t:
        k -= 1
    while (k + 1) * resolution <= t:
        k += 1
    return k


def cell_ceil(t, resolution):
    k = ceil(t / resolution)
    while k * resolution < t:
        k += 1
    while (k - 1) * resolution >= t:
        k -= 1
    return k


class PartialCoverage(Mapping):
    """Bound resident geometry; callers can stream each cell's pieces."""

    def __init__(self):
        self._file = tempfile.SpooledTemporaryFile(max_size=65536)
        self._index = {}

    def append(self, cell, lo, hi):
        offset = self._file.tell()
        first, count = self._index.get(cell, (offset, 0))
        self._file.write(_PAIR.pack(lo, hi))
        self._index[cell] = first, count + 1

    def pieces(self, cell):
        first, count = self._index[cell]
        for i in range(count):
            self._file.seek(first + i * _PAIR.size)
            yield _PAIR.unpack(self._file.read(_PAIR.size))

    def __getitem__(self, cell):
        return tuple(self.pieces(cell))

    def __iter__(self):
        return iter(self._index)

    def __len__(self):
        return len(self._index)

    def close(self):
        self._file.close()

    def __del__(self):
        self.close()


def _pieces(partial, cell):
    if isinstance(partial, PartialCoverage):
        return partial.pieces(cell)
    return iter(partial[cell])


@dataclass(frozen=True, slots=True)
class DayDigest:
    resolution: int
    k0: int
    n: int
    channels: tuple[bytes, ...]
    cov: bytes
    partial: Mapping
    physical: object = None

    def segments(self):
        """Yield merged coverage without retaining all partial pieces."""
        pending = None
        for lo, hi in self._intervals():
            if pending is not None and lo <= pending[1]:
                pending = pending[0], max(hi, pending[1])
            else:
                if pending is not None:
                    yield pending
                pending = lo, hi
        if pending is not None:
            yield pending

    def _intervals(self):
        for cell, state in enumerate(self.cov):
            if state == 1:
                yield (self.k0 + cell) * self.resolution, (self.k0 + cell + 1) * self.resolution
            elif state == 2:
                yield from _pieces(self.partial, cell)


class _Folder:
    def __init__(self, start, end, selected):
        self.k0 = cell_floor(start, CELL_SECONDS)
        self.n = cell_ceil(end, CELL_SECONDS) - self.k0
        self.columns = [bytearray([MISSING]) * self.n for _ in range(CHANNELS)]
        self.cov = bytearray(self.n)
        self.partial = PartialCoverage()
        self.selected = selected
        self.pending = None
        self.physical = PhysicalBuilder(selected, start, end)

    def coverage(self, lo, hi):
        if self.pending is not None and lo == self.pending[1]:
            self.pending = self.pending[0], hi
        else:
            self.flush()
            self.pending = lo, hi

    def flush(self):
        if self.pending is None:
            return
        lo, hi = self.pending
        first, after = cell_ceil(lo, CELL_SECONDS), cell_floor(hi, CELL_SECONDS)
        if after > first:
            self.cov[first - self.k0 : after - self.k0] = b"\x01" * (after - first)
        if lo < first * CELL_SECONDS:
            self._partial(first - 1, lo, min(hi, first * CELL_SECONDS))
        if hi > after * CELL_SECONDS and after >= first:
            self._partial(after, max(lo, after * CELL_SECONDS), hi)
        self.pending = None

    def _partial(self, k, lo, hi):
        cell = k - self.k0
        self.cov[cell] = 2
        self.partial.append(cell, lo, hi)

    def row(self, lo, hi, values):
        self.coverage(lo, hi)
        for k in range(cell_floor(lo, CELL_SECONDS), cell_ceil(hi, CELL_SECONDS)):
            cell = k - self.k0
            for c in self.selected:
                old = self.columns[c][cell]
                self.columns[c][cell] = values[c] if old == MISSING else max(old, values[c])


def _fold_rows(folder, block, start, end, first, after):
    flags = int.from_bytes(bytes(map(ge, block.times[first:after], repeat(start))), "little")
    flags &= int.from_bytes(bytes(map(le, block.ends[first:after], repeat(end))), "little")
    flags &= int.from_bytes(
        bytes(map(lt, block.times[first:after], block.ends[first:after])), "little"
    )
    for c in folder.selected:
        flags &= int.from_bytes(block.columns[c][first:after].translate(_USABLE), "little")
    previous = first
    for match in finditer(b"\x01+", flags.to_bytes(after - first, "little")):
        a, b = first + match.start(), first + match.end()
        _clipped_rows(folder, block, start, end, previous, a)
        for lo, hi in _contiguous(block, a, b):
            _reduce_cells(folder, block, lo, hi)
        previous = b
    _clipped_rows(folder, block, start, end, previous, after)


def _clipped_rows(folder, block, start, end, first, after):
    for i in range(first, after):
        lo, hi = max(start, block.times[i]), min(end, block.ends[i])
        values = {c: block.columns[c][i] for c in folder.selected}
        if hi > lo and MISSING not in values.values():
            folder.row(lo, hi, values)


def _cell_rows(block, first, after, cells):
    """Each cell's rows of a contiguous range, as slices; no Python step per row."""
    edges = list(map(mul, cells, repeat(CELL_SECONDS)))
    starts = map(bisect_right, repeat(block.ends), edges, repeat(first), repeat(after))
    edges = map(add, edges, repeat(CELL_SECONDS))
    stops = map(bisect_left, repeat(block.times), edges, repeat(first), repeat(after))
    return list(map(slice, starts, stops))


def _reduce_cells(folder, block, first, after):
    """Reduce contiguous usable rows in C, including bursts and offset cells."""
    lo, hi = block.times[first], block.ends[after - 1]
    folder.coverage(lo, hi)
    cells = range(cell_floor(lo, CELL_SECONDS), cell_ceil(hi, CELL_SECONDS))
    rows = _cell_rows(block, first, after, cells)
    start, stop = cells.start - folder.k0, cells.stop - folder.k0
    missing = bytes([MISSING]) * len(rows)
    for c in folder.selected:
        values = bytes(map(max, map(block.columns[c].__getitem__, rows)))
        old = folder.columns[c][start:stop]
        if old != missing:
            values = bytes(map(max, old.translate(_ZERO_MISSING), values))
        folder.columns[c][start:stop] = values


def _full_batches(block, start, end, selected):
    flags = int.from_bytes(
        bytes(map(eq, map(sub, block.ends, block.times), repeat(CELL_SECONDS))), "little"
    )
    flags &= int.from_bytes(
        bytes(map(eq, map(mod, block.times, repeat(CELL_SECONDS)), repeat(0))), "little"
    )
    flags &= int.from_bytes(bytes(map(ge, block.times, repeat(start))), "little")
    flags &= int.from_bytes(bytes(map(le, block.ends, repeat(end))), "little")
    for c in selected:
        flags &= int.from_bytes(block.columns[c].translate(_USABLE), "little")
    for match in finditer(b"\x01+", flags.to_bytes(len(block.times), "little")):
        yield from _contiguous(block, match.start(), match.end())


def _contiguous(block, first, after):
    offset = first + 1
    joined = bytes(map(eq, block.times[offset:after], block.ends[first : after - 1]))
    for match in finditer(b"\x00", joined):
        stop = offset + match.start()
        yield first, stop
        first = stop
    yield first, after


def _copy_cells(folder, block, first, after):
    lo, hi = block.times[first], block.ends[after - 1]
    folder.coverage(lo, hi)
    cell = cell_floor(lo, CELL_SECONDS) - folder.k0
    stop = cell + after - first
    missing = bytes([MISSING]) * (after - first)
    for c in folder.selected:
        old = folder.columns[c][cell:stop]
        values = block.columns[c][first:after]
        if old != missing:
            values = bytes(map(max, old.translate(_ZERO_MISSING), values))
        folder.columns[c][cell:stop] = values


def _fold_block(folder, block, start, end):
    folder.physical.add(block)
    previous = 0
    for first, after in _full_batches(block, start, end, folder.selected):
        _fold_rows(folder, block, start, end, previous, first)
        _copy_cells(folder, block, first, after)
        previous = after
    _fold_rows(folder, block, start, end, previous, len(block.times))


def fold_day(blocks, start, end, channels):
    """Fold disjoint chronological blocks, retaining only one day's cells."""
    if not isfinite(start) or not isfinite(end) or end <= start:
        raise ValueError("Invalid day bounds")
    if not channels or any(c not in range(CHANNELS) for c in channels):
        raise ValueError("Invalid selected channels")
    folder = _Folder(start, end, channels)
    try:
        for block in blocks:
            _fold_block(folder, block, start, end)
        folder.flush()
        return DayDigest(
            CELL_SECONDS,
            folder.k0,
            folder.n,
            tuple(bytes(c) for c in folder.columns),
            bytes(folder.cov),
            folder.partial,
            physical=folder.physical.finish(),
        )
    except BaseException:
        folder.partial.close()
        raise
