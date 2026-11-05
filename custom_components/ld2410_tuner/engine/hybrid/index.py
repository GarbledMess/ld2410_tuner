"""Label-independent per-day energy buckets with bounded position masks."""

import struct
import sys
from array import array
from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass
from operator import lt

_POSITION_BYTES = b"".join(bytes((128 | (i & 127), 128 | (i >> 7))) for i in range(16384))
_MATCH_TABLES = tuple(bytes(255 if v == e else 0 for v in range(256)) for e in range(101))


@dataclass(frozen=True, slots=True)
class EnergyIndex:
    buckets: tuple
    bounds: tuple
    n: int

    @classmethod
    def build(cls, columns):
        buckets = tuple(_column(column) for column in columns)
        bounds = tuple((min(b, default=255), max(b, default=255)) for b in buckets)
        return cls(buckets, bounds, len(columns[0]) if columns else 0)

    def nbytes(self):
        return sum(len(v) * v.itemsize for b in self.buckets for v in b.values())

    def band(self, channel, low, high, lo=0, hi=None):
        hi = self.n if hi is None else min(self.n, hi)
        result = 0
        for energy, positions in self.buckets[channel].items():
            if low < energy <= high:
                a, b = bisect_left(positions, lo), bisect_left(positions, hi)
                result |= _bits(positions[a:b], lo)
        return result

    def mask(self, channel, threshold, lo=0, hi=None):
        return self.band(channel, threshold, 100, lo, hi)


def _column(column):
    energies = {energy for energy in set(column) if energy <= 100}
    if len(column) <= 16384 and len(energies) <= 12:
        return _compact_positions(column, energies)
    result = defaultdict(list)
    for position, energy in enumerate(column):
        if energy <= 100:
            result[energy].append(position)
    return {energy: array("H", positions) for energy, positions in result.items()}


def _compact_positions(column, energies):
    n = len(column)
    doubled = bytearray(n * 2)
    doubled[::2] = doubled[1::2] = column
    positions = int.from_bytes(_POSITION_BYTES[: n * 2], "little")
    low = int.from_bytes(b"\x7f\x00" * n, "little")
    result = {}
    for energy in energies:
        selected = positions & int.from_bytes(doubled.translate(_MATCH_TABLES[energy]), "little")
        packed = selected.to_bytes(n * 2, "little").translate(None, b"\x00")
        # Nonzero base-128 digits let translate delete unselected positions in C.
        # Remove their marker bits and move the high digit down one bit to uint16.
        bits = int.from_bytes(packed, "little")
        raw = ((bits & low) | ((bits & (low << 8)) >> 1)).to_bytes(len(packed), "little")
        values = array("H", raw)
        if sys.byteorder != "little":
            values.byteswap()
        result[energy] = values
    return result


def _bits(positions, lo):
    if not positions:
        return 0
    result = bytearray((positions[-1] - lo) // 8 + 1)
    for position in positions:
        at = position - lo
        result[at // 8] |= 1 << (at % 8)
    return int.from_bytes(result, "little")


def write_index(stream, index):
    stream.write(struct.pack("<B", len(index.buckets)))
    for buckets in index.buckets:
        stream.write(struct.pack("<B", len(buckets)))
        for energy, positions in sorted(buckets.items()):
            stream.write(struct.pack("<BH", energy, len(positions)))
            if sys.byteorder != "little":
                positions = array("H", positions)
                positions.byteswap()
            stream.write(positions.tobytes())


def read_index(stream, n):
    channels = _read(stream, 1)[0]
    if channels != 18:
        raise ValueError("Invalid energy index channels")
    buckets = tuple(_read_column(stream, n) for _ in range(channels))
    bounds = tuple((min(b, default=255), max(b, default=255)) for b in buckets)
    return EnergyIndex(buckets, bounds, n)


def _read_column(stream, n):
    result = {}
    size = _read(stream, 1)[0]
    if size > 101:
        raise ValueError("Invalid energy buckets")
    for _ in range(size):
        energy, count = struct.unpack("<BH", _read(stream, 3))
        positions = array("H", _read(stream, 2 * count))
        if sys.byteorder != "little":
            positions.byteswap()
        if energy > 100 or energy in result or (positions[-1] if positions else -1) >= n:
            raise ValueError("Invalid energy positions")
        if not all(map(lt, positions, positions[1:])):
            raise ValueError("Unordered energy positions")
        result[energy] = positions
    return result


def _read(stream, n):
    value = stream.read(n)
    if len(value) != n:
        raise ValueError("Truncated energy index")
    return value
