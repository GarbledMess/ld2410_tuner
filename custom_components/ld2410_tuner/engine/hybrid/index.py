"""Label-independent per-day energy buckets with bounded position masks."""

import struct
from array import array
from bisect import bisect_left
from dataclasses import dataclass


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
    result = {}
    for position, energy in enumerate(column):
        if energy <= 100:
            result.setdefault(energy, array("H")).append(position)
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
            stream.write(struct.pack("<" + "H" * len(positions), *positions))


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
        positions = array("H", struct.unpack("<" + "H" * count, _read(stream, 2 * count)))
        if energy > 100 or energy in result or any(p >= n for p in positions):
            raise ValueError("Invalid energy positions")
        if any(a >= b for a, b in zip(positions, positions[1:], strict=False)):
            raise ValueError("Unordered energy positions")
        result[energy] = positions
    return result


def _read(stream, n):
    value = stream.read(n)
    if len(value) != n:
        raise ValueError("Truncated energy index")
    return value
