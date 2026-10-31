"""Label-independent six-second maxima and spooled exact coverage geometry."""

import struct
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from math import ceil, floor, isfinite

from .constants import CELL_SECONDS, CHANNELS, MISSING
from .physical_index import PhysicalBuilder

_PAIR = struct.Struct("<dd")


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
    index: object = None
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
        for k in range(cell_floor(lo, CELL_SECONDS), cell_ceil(hi, CELL_SECONDS)):
            a, b = max(lo, k * CELL_SECONDS), min(hi, (k + 1) * CELL_SECONDS)
            cell = k - self.k0
            if b - a == CELL_SECONDS:
                self.cov[cell] = 1
            else:
                self.cov[cell] = 2
                self.partial.append(cell, a, b)
        self.pending = None

    def row(self, lo, hi, values):
        self.coverage(lo, hi)
        for k in range(cell_floor(lo, CELL_SECONDS), cell_ceil(hi, CELL_SECONDS)):
            cell = k - self.k0
            for c in self.selected:
                old = self.columns[c][cell]
                self.columns[c][cell] = values[c] if old == MISSING else max(old, values[c])


def _fold_block(folder, block, start, end):
    folder.physical.add(block)
    for i, (lo, hi) in enumerate(zip(block.times, block.ends, strict=True)):
        lo, hi = max(start, lo), min(end, hi)
        if hi <= lo:
            continue
        values = {c: block.columns[c][i] for c in folder.selected}
        if MISSING not in values.values():
            folder.row(lo, hi, values)


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
