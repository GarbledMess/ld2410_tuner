"""Label-independent physical rows of one radar-day, persisted beside its cells.

Only usable rows are kept: positive duration with every selected channel read.
Contiguous rows with an identical selected vector are coalesced; neither change
alters a strict crossing, a coverage segment or any timing input. Geometry and
energies compress separately, and a decoded day is just C arrays, so exact replay
never re-decodes the day file or loops over rows to read them.
"""

import struct
import sys
import zlib
from array import array
from bisect import bisect_left, bisect_right
from itertools import accumulate, compress, repeat
from operator import add, eq, lt, sub, truediv

from .constants import MISSING

_HEADER = struct.Struct("<IBII")
_INVALID = bytes(1 if v == MISSING else 0 for v in range(256))
_USABLE = bytes(0 if v == MISSING else 1 for v in range(256))


def _native(values):
    if sys.byteorder != "little":
        values.byteswap()
    return values


class PhysicalRows:
    """One day's usable physical rows; decoding is lazy and validated once."""

    __slots__ = ("_columns", "_encoded", "_ends", "_times", "channels", "count")

    def __init__(self, channels, times, ends, columns):
        self.channels = tuple(sorted(channels))
        self._times, self._ends, self._columns = times, ends, columns
        self._encoded = None
        self.count = 0 if times is None else len(times)

    @classmethod
    def decode(cls, channels, encoded):
        """Keep the compressed sections until a replay or row budget needs them."""
        if len(encoded) < _HEADER.size:
            raise ValueError("Truncated physical rows")
        rows = cls(channels, None, None, None)
        rows.count = _HEADER.unpack_from(encoded)[0]
        rows._encoded = bytes(encoded)
        return rows

    @property
    def decoded(self):
        return self._encoded is None

    def _decode(self):
        if self._encoded is None:
            return
        count, milliseconds, geometry, energy = _HEADER.unpack_from(self._encoded)
        at = _HEADER.size
        if len(self._encoded) != at + geometry + energy:
            raise ValueError("Invalid physical rows size")
        try:
            times, ends = _geometry(
                zlib.decompress(self._encoded[at : at + geometry]), milliseconds
            )
            raw = zlib.decompress(self._encoded[at + geometry :])
        except zlib.error as error:
            raise ValueError("Corrupt physical rows") from error
        if len(times) != count or len(raw) != count * len(self.channels):
            raise ValueError("Invalid physical rows shape")
        columns = {c: raw[i * count : (i + 1) * count] for i, c in enumerate(self.channels)}
        self._times, self._ends, self._columns = times, ends, columns
        self._encoded = None
        self.validate()

    def validate(self):
        times, ends = self._times, self._ends
        if len(ends) != len(times) or any(len(v) != len(times) for v in self._columns.values()):
            raise ValueError("Invalid physical rows shape")
        if not all(map(lt, times, ends)) or any(map(lt, times[1:], ends[:-1])):
            raise ValueError("Invalid physical rows geometry")
        if any(column.translate(_INVALID).count(1) for column in self._columns.values()):
            raise ValueError("Invalid physical rows energy")

    def geometry(self):
        self._decode()
        return self._times, self._ends

    def rows_in(self, lo, hi):
        if hi <= lo:
            return 0
        times, ends = self.geometry()
        return max(0, bisect_left(times, hi) - bisect_right(ends, lo))

    def window(self, lo, hi):
        """Rows overlapping ``[lo, hi)``, clipped; columns cover the selected channels."""
        times, ends = self.geometry()
        if hi <= lo:
            return array("d"), array("d"), dict.fromkeys(self.channels, b"")
        a, b = bisect_right(ends, lo), bisect_left(times, hi)
        if b <= a:
            return array("d"), array("d"), dict.fromkeys(self.channels, b"")
        t, e = times[a:b], ends[a:b]
        t[0], e[-1] = max(t[0], lo), min(e[-1], hi)
        return t, e, {c: column[a:b] for c, column in self._columns.items()}

    def encode(self):
        times, ends = self.geometry()
        milliseconds, geometry = _encode_geometry(times, ends)
        geometry = zlib.compress(geometry, 1)
        energy = zlib.compress(b"".join(self._columns[c] for c in self.channels), 1)
        header = _HEADER.pack(self.count, milliseconds, len(geometry), len(energy))
        return header + geometry + energy

    def nbytes(self):
        if self._encoded is not None:
            return len(self._encoded)
        return 16 * self.count + sum(len(v) for v in self._columns.values())


def _exact_milliseconds(values):
    scaled = [round(v * 1000) for v in values]
    return scaled if all(map(eq, map(truediv, scaled, repeat(1000)), values)) else None


def _encode_geometry(times, ends):
    starts, stops = _exact_milliseconds(times), _exact_milliseconds(ends)
    if starts is None or stops is None:
        return 0, _native(array("d", times)).tobytes() + _native(array("d", ends)).tobytes()
    steps = array("q", starts[:1])
    steps.extend(map(sub, starts[1:], starts[:-1]))
    lengths = array("q", map(sub, stops, starts))
    return 1, _native(steps).tobytes() + _native(lengths).tobytes()


def _geometry(raw, milliseconds):
    if len(raw) % 16:
        raise ValueError("Invalid physical geometry")
    half = len(raw) // 2
    if not milliseconds:
        return _native(array("d", raw[:half])), _native(array("d", raw[half:]))
    steps, lengths = _native(array("q", raw[:half])), _native(array("q", raw[half:]))
    starts = list(accumulate(steps))
    stops = map(add, starts, lengths)
    return (
        array("d", map(truediv, starts, repeat(1000))),
        array("d", map(truediv, stops, repeat(1000))),
    )


class PhysicalBuilder:
    """Accumulate one day's usable rows from chronological reader blocks."""

    def __init__(self, channels, start, end):
        self.channels = tuple(sorted(channels))
        self.start, self.end = start, end
        self.times, self.ends = array("d"), array("d")
        self.columns = {c: bytearray() for c in self.channels}

    def add(self, block):
        n = len(block.times)
        if not n:
            return
        times = array("d", map(max, block.times, repeat(self.start)))
        ends = array("d", map(min, block.ends, repeat(self.end)))
        keep = int.from_bytes(bytes(map(lt, times, ends)), "little")
        for c in self.channels:
            keep &= int.from_bytes(block.columns[c].translate(_USABLE), "little")
        if not keep:
            return
        flags = keep.to_bytes(n, "little")
        selected = [bytes(compress(block.columns[c], flags)) for c in self.channels]
        self._coalesce(
            array("d", compress(times, flags)), array("d", compress(ends, flags)), selected
        )

    def _coalesce(self, times, ends, selected):
        vectors = list(zip(*selected, strict=True))
        previous = self._last()
        new = [previous is None or times[0] != previous[0] or vectors[0] != previous[1]]
        new.extend(map(_fresh, times[1:], ends[:-1], vectors[1:], vectors[:-1]))
        starts = list(compress(range(len(new)), new))
        if not new[0]:
            self.ends[-1] = ends[starts[0] - 1] if starts else ends[-1]
        stops = [*starts[1:], len(times)]
        self.times.extend(times[i] for i in starts)
        self.ends.extend(ends[i - 1] for i in stops)
        for c, column in zip(self.channels, selected, strict=True):
            self.columns[c].extend(column[i] for i in starts)

    def _last(self):
        if not self.times:
            return None
        return self.ends[-1], tuple(self.columns[c][-1] for c in self.channels)

    def finish(self):
        return PhysicalRows(
            self.channels,
            self.times,
            self.ends,
            {c: bytes(v) for c, v in self.columns.items()},
        )


def _fresh(time, end, vector, previous):
    return time != end or vector != previous


def physical_rows(blocks, start, end, channels):
    """Build one day's rows from reader blocks clipped to ``[start, end)``."""
    builder = PhysicalBuilder(channels, start, end)
    for block in blocks:
        builder.add(block)
    return builder.finish()
