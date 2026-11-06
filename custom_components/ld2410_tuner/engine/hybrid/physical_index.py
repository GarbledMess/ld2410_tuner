"""Label-independent physical rows of one radar-day, persisted beside its cells.

Only usable rows are kept: positive duration with every selected channel read.
Contiguous rows with an identical selected vector are coalesced; neither change
alters a strict crossing, a coverage segment or any timing input.

Rows are stored as compressed blocks of at most ``PHYSICAL_BLOCK_ROWS`` rows in a
spooled temporary file, so neither building nor reading a day keeps more than one
block of plain rows per day resident, however many rows the day has. A small
index of each block's first and last start and end answers row counts without
decoding interior blocks; windows decode only the blocks they overlap, through
one process-wide decoded-block cache bounded by ``DECODED_BLOCK_BYTES``.
Validated native buffers of partly filled blocks also have a compressed ``SPILL_BYTES``
cache, so plain block eviction need not repeat delta decoding or geometry validation. A full
block is left out: compressing it costs more than decoding it again, and a chronological
sweep over a long history would evict it before its next use.
"""

import struct
import sys
import tempfile
import zlib
from array import array
from bisect import bisect_left, bisect_right
from collections import OrderedDict
from io import BytesIO
from itertools import accumulate, compress, count, islice, repeat
from operator import add, eq, lt, ne, sub, truediv

from .constants import MISSING, SPILL_BYTES

PHYSICAL_BLOCK_ROWS = 16384
DECODED_BLOCK_BYTES = 16 * 1024**2
SPOOL_BYTES = 1024**2
COPY_BYTES = 1024**2
_MAGIC = b"PRB1"
_DAY = struct.Struct("<4sII")
_BLOCK = struct.Struct("<IBIIdddd")
_USABLE = bytes(0 if v == MISSING else 1 for v in range(256))
_NONZERO = bytes(bool(v) for v in range(256))
_TOKENS = count()
_DECODED = OrderedDict()
_DECODED_BYTES = [0]


class _CompiledBlocks:
    """Compressed native buffers of validated blocks, bounded independently of rows."""

    def __init__(self):
        self.entries, self.size = OrderedDict(), 0

    def get(self, key, channels):
        found = self.entries.get(key)
        if found is None:
            return None
        self.entries.move_to_end(key)
        rows, encoded, _size = found
        raw = zlib.decompress(encoded)
        geometry = bytearray(16 * rows)
        for k in range(8):
            geometry[k::8] = raw[k * 2 * rows : (k + 1) * 2 * rows]
        times = array("d", geometry[: 8 * rows])
        ends = array("d", geometry[8 * rows :])
        columns = {c: raw[(16 + i) * rows : (17 + i) * rows] for i, c in enumerate(channels)}
        return times, ends, columns

    def put(self, key, value):
        times, ends, columns = value
        geometry = times.tobytes() + ends.tobytes()
        raw = b"".join(geometry[k::8] for k in range(8)) + b"".join(columns.values())
        encoded = zlib.compress(raw, 1)
        size = len(encoded) + 512
        if size > SPILL_BYTES:
            return
        while self.entries and self.size + size > SPILL_BYTES:
            _, (_, _, removed) = self.entries.popitem(last=False)
            self.size -= removed
        self.entries[key] = len(times), encoded, size
        self.size += size


_COMPILED = _CompiledBlocks()


def _native(values):
    if sys.byteorder != "little":
        values.byteswap()
    return values


class _FileSection:
    """A read-only part of a file, opened only for each read: no handle outlives a call."""

    def __init__(self, path, base):
        self.path, self.base, self.at = path, base, 0

    def seek(self, at, _whence=0):
        self.at = at

    def read(self, size):
        try:
            with open(self.path, "rb") as stream:
                stream.seek(self.base + self.at)
                data = stream.read(size)
        except OSError as error:
            raise ValueError("Unreadable physical rows") from error
        self.at += len(data)
        return data

    def close(self):
        """Nothing stays open."""


class PhysicalRows:
    """One day's usable physical rows as indexed compressed blocks.

    ``index`` holds per block ``(offset, count, first time, last time, first end,
    last end)``; a decoded section's index is read from its block headers only on
    first use, and each block's rows are validated whenever it is decoded.
    """

    __slots__ = ("_index", "_size", "_spool", "_token", "channels", "count")

    def __init__(self, channels, times, ends, columns):
        self.channels = tuple(sorted(channels))
        self._spool = tempfile.SpooledTemporaryFile(max_size=SPOOL_BYTES)
        self._token = next(_TOKENS)
        self._index, self._size, self.count = [], 0, 0
        if times is not None:
            for at in range(0, len(times), PHYSICAL_BLOCK_ROWS):
                stop = at + PHYSICAL_BLOCK_ROWS
                part = {c: columns[c][at:stop] for c in self.channels}
                self._append(times[at:stop], ends[at:stop], part)

    def share_decoded(self, token):
        """Name these rows, so every copy read from one source shares its decoded blocks."""
        self._token = token

    def _append(self, times, ends, columns):
        if not len(times):
            return
        payload = _encode_block(self.channels, times, ends, columns)
        self._spool.seek(0, 2)
        self._index.append((self._spool.tell(), len(times), times[0], times[-1], ends[0], ends[-1]))
        self._spool.write(payload)
        self._size += len(payload)
        self.count += len(times)

    @classmethod
    def decode(cls, channels, encoded):
        """Rows of an encoded section; its block index is read on first use."""
        return cls.read(_Bytes(encoded), len(encoded), channels)

    @classmethod
    def read(cls, stream, length, channels):
        """Copy ``length`` encoded bytes from ``stream`` in bounded pieces."""
        if length < _DAY.size:
            raise ValueError("Truncated physical rows")
        rows = cls(channels, None, None, None)
        head = stream.read(_DAY.size)
        if len(head) != _DAY.size:
            raise ValueError("Truncated physical rows")
        magic, rows.count, _blocks = _DAY.unpack(head)
        remaining = length - _DAY.size
        while remaining:
            piece = stream.read(min(COPY_BYTES, remaining))
            if not piece:
                raise ValueError("Truncated physical rows")
            rows._spool.write(piece)
            remaining -= len(piece)
        rows._size, rows._index = length - _DAY.size, None
        if magic != _MAGIC:
            rows._index = ValueError("Invalid physical rows header")
        return rows

    def place(self, path, offset):
        """What ``at_place`` needs to read these rows again where ``read`` found them."""
        return str(path), offset + _DAY.size, self.channels, self.count, tuple(self._blocks())

    @classmethod
    def at_place(cls, place, size, token):
        """These rows from their file, copying nothing until a block is asked for."""
        rows = cls.__new__(cls)
        path, base, rows.channels, rows.count, blocks = place
        rows._index, rows._size, rows._token = list(blocks), size - _DAY.size, token
        rows._spool = _FileSection(path, base)
        return rows

    @property
    def decoded(self):
        """Whether the block index is known (always, unless read and not yet used)."""
        return isinstance(self._index, list)

    def _blocks(self):
        if not self.decoded:
            self._index = self._read_index()
        return self._index

    def _read_index(self):
        if isinstance(self._index, Exception):
            raise self._index
        index, at, total = [], 0, 0
        while at < self._size:
            self._spool.seek(at)
            head = self._spool.read(_BLOCK.size)
            if len(head) != _BLOCK.size:
                raise ValueError("Truncated physical block")
            rows, _ms, geometry, energy, *bounds = _BLOCK.unpack(head)
            if not rows or index and bounds[0] < index[-1][5]:
                raise ValueError("Invalid physical rows geometry")
            index.append((at, rows, *bounds))
            at += _BLOCK.size + geometry + energy
            total += rows
        if at != self._size or total != self.count:
            raise ValueError("Invalid physical rows shape")
        return index

    def block(self, k):
        """Decoded ``(times, ends, columns)`` of block ``k``, validated."""
        key = self._token, k
        if key in _DECODED:
            _DECODED.move_to_end(key)
            return _DECODED[key][0]
        value = _COMPILED.get(key, self.channels)
        if value is not None:
            _remember(key, value, len(value[0]) * (16 + len(self.channels)) + 256)
            return value
        offset, rows, *bounds = self._blocks()[k]
        self._spool.seek(offset)
        head = self._spool.read(_BLOCK.size)
        _, milliseconds, geometry, energy, *_ = _BLOCK.unpack(head)
        payload = self._spool.read(geometry + energy)
        value = _decode_block(self.channels, rows, milliseconds, geometry, payload, bounds)
        if rows < PHYSICAL_BLOCK_ROWS:
            _COMPILED.put(key, value)
        _remember(key, value, rows * (16 + len(self.channels)) + 256)
        return value

    def validate(self):
        """Decode and check every block, one at a time."""
        for k in range(len(self._blocks())):
            self.block(k)

    def geometry(self):
        """Every row's start and end (whole day; prefer ``window`` and ``rows_in``)."""
        times, ends = array("d"), array("d")
        for k in range(len(self._blocks())):
            t, e, _ = self.block(k)
            times.extend(t)
            ends.extend(e)
        return times, ends

    def rows_in(self, lo, hi):
        if hi <= lo:
            return 0
        started = ended = 0
        for k, (_offset, rows, first, last, first_end, last_end) in enumerate(self._blocks()):
            started += self._below(k, rows, first, last, hi, 0)
            ended += self._below(k, rows, first_end, last_end, lo, 1)
        return max(0, started - ended)

    def _below(self, k, rows, first, last, value, side):
        """Rows of block ``k`` whose start (side 0) is below / end (1) at most ``value``."""
        if (first >= value) if side == 0 else (first > value):
            return 0
        if (last < value) if side == 0 else (last <= value):
            return rows
        values = self.block(k)[side]
        return bisect_left(values, value) if side == 0 else bisect_right(values, value)

    def window(self, lo, hi):
        """Rows overlapping ``[lo, hi)``, clipped; columns cover the selected channels."""
        times, ends = array("d"), array("d")
        columns = {c: bytearray() for c in self.channels}
        if hi > lo:
            for k, (_offset, _rows, first, _last, _first_end, last_end) in enumerate(
                self._blocks()
            ):
                if last_end > lo and first < hi:
                    _extend_window(self.block(k), lo, hi, times, ends, columns)
        if times:
            times[0], ends[-1] = max(times[0], lo), min(ends[-1], hi)
        return times, ends, {c: bytes(v) for c, v in columns.items()}

    def write(self, stream):
        """Write the encoded section to ``stream`` in bounded pieces."""
        blocks = self._blocks()
        stream.write(_DAY.pack(_MAGIC, self.count, len(blocks)))
        self._spool.seek(0)
        remaining = self._size
        while remaining:
            piece = self._spool.read(min(COPY_BYTES, remaining))
            stream.write(piece)
            remaining -= len(piece)

    def encoded_size(self):
        return _DAY.size + self._size

    def snapshot(self):
        """Immutable compressed storage and block identity for independent readers."""
        blocks = tuple(self._blocks())
        self._spool.seek(0)
        return self.channels, self.count, blocks, self._spool.read(self._size), self._token

    @classmethod
    def from_snapshot(cls, snapshot):
        rows = cls.__new__(cls)
        rows.channels, rows.count, blocks, data, rows._token = snapshot
        rows._index, rows._size = list(blocks), len(data)
        rows._spool = BytesIO(data)
        return rows

    def encode(self):
        out = _Bytes(b"")
        self.write(out)
        return out.value()

    def nbytes(self):
        return self.encoded_size()

    def close(self):
        self._spool.close()


class _Bytes:
    """A minimal in-memory stream for ``decode`` and ``encode``."""

    def __init__(self, data):
        self._data, self._at, self._out = data, 0, []

    def read(self, size):
        piece = self._data[self._at : self._at + size]
        self._at += len(piece)
        return piece

    def write(self, data):
        self._out.append(bytes(data))

    def value(self):
        return b"".join(self._out)


def _remember(key, value, size):
    _DECODED[key] = value, size
    _DECODED_BYTES[0] += size
    while len(_DECODED) > 1 and _DECODED_BYTES[0] > DECODED_BLOCK_BYTES:
        _, (_, removed) = _DECODED.popitem(last=False)
        _DECODED_BYTES[0] -= removed


def _extend_window(block, lo, hi, times, ends, columns):
    t, e, values = block
    a, b = bisect_right(e, lo), bisect_left(t, hi)
    if b <= a:
        return
    times.extend(t[a:b])
    ends.extend(e[a:b])
    for c, column in values.items():
        columns[c] += column[a:b]


def _encode_block(channels, times, ends, columns):
    milliseconds, geometry = _encode_geometry(times, ends)
    geometry = zlib.compress(geometry, 1)
    energy = zlib.compress(b"".join(columns[c] for c in channels), 1)
    bounds = times[0], times[-1], ends[0], ends[-1]
    head = _BLOCK.pack(len(times), milliseconds, len(geometry), len(energy), *bounds)
    return head + geometry + energy


def _decode_block(channels, rows, milliseconds, geometry, payload, bounds):
    try:
        times, ends, verified = _decoded_geometry(zlib.decompress(payload[:geometry]), milliseconds)
        raw = zlib.decompress(payload[geometry:])
    except zlib.error as error:
        raise ValueError("Corrupt physical rows") from error
    if len(times) != rows or len(raw) != rows * len(channels):
        raise ValueError("Invalid physical rows shape")
    if (times[0], times[-1], ends[0], ends[-1]) != tuple(bounds):
        raise ValueError("Invalid physical block bounds")
    columns = {c: raw[i * rows : (i + 1) * rows] for i, c in enumerate(channels)}
    _validate(times, ends, columns, geometry_verified=verified)
    return times, ends, columns


def _validate(times, ends, columns, *, geometry_verified=False):
    if len(ends) != len(times) or any(len(v) != len(times) for v in columns.values()):
        raise ValueError("Invalid physical rows shape")
    if not geometry_verified and (
        not all(map(lt, times, ends)) or any(map(lt, times[1:], ends[:-1]))
    ):
        raise ValueError("Invalid physical rows geometry")
    if any(MISSING in column for column in columns.values()):
        raise ValueError("Invalid physical rows energy")


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
    return _decoded_geometry(raw, milliseconds)[:2]


def _decoded_geometry(raw, milliseconds):
    if len(raw) % 16:
        raise ValueError("Invalid physical geometry")
    half = len(raw) // 2
    if not milliseconds:
        return _native(array("d", raw[:half])), _native(array("d", raw[half:])), False
    steps, lengths = _native(array("q", raw[:half])), _native(array("q", raw[half:]))
    regular = _regular_geometry(steps, lengths)
    if regular is not None:
        return *regular, 0 < lengths[0] <= steps[1]
    starts = list(accumulate(steps))
    stops = map(add, starts, lengths)
    # Sized lists let array allocate once instead of growing for each decoded value.
    return (
        array("d", list(map(truediv, starts, repeat(1000)))),
        array("d", list(map(truediv, stops, repeat(1000)))),
        False,
    )


def _regular_geometry(steps, lengths):
    if len(steps) < 2:
        return None
    first, step, length = steps[0], steps[1], lengths[0]
    if first % 1000 or step <= 0 or length < 0 or step % 125 or length % 125:
        return None
    if not _repeated_integer(steps, 1) or not _repeated_integer(lengths, 0):
        return None
    last = first + step * (len(steps) - 1) + length
    if max(abs(first), abs(last)) > 2**50:
        return None
    # Multiples of 1/8 second remain exactly representable throughout this range.
    start, stride, duration = first / 1000, step / 1000, length / 1000
    return (
        array("d", list(islice(count(start, stride), len(steps)))),
        array("d", list(islice(count(start + duration, stride), len(steps)))),
    )


def _repeated_integer(values, offset):
    """Compare encoded integers in C without boxing one Python value per row."""
    encoded = values.tobytes()
    width = values.itemsize
    token = encoded[offset * width : (offset + 1) * width]
    return encoded[offset * width :] == token * (len(values) - offset)


class PhysicalBuilder:
    """Accumulate one day's usable rows from chronological reader blocks.

    Pending rows are flushed to the day's spooled blocks once more than one block
    is waiting; the last row always stays pending so coalescing never needs a row
    already written, and block edges fall on fixed row counts.
    """

    def __init__(self, channels, start, end):
        self.channels = tuple(sorted(channels))
        self.start, self.end = start, end
        self.rows = PhysicalRows(self.channels, None, None, None)
        self._reset()

    def _reset(self):
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
        while len(self.times) > PHYSICAL_BLOCK_ROWS:
            self._flush(PHYSICAL_BLOCK_ROWS)

    def _flush(self, rows):
        part = {c: bytes(v[:rows]) for c, v in self.columns.items()}
        self.rows._append(self.times[:rows], self.ends[:rows], part)
        del self.times[:rows], self.ends[:rows]
        for column in self.columns.values():
            del column[:rows]

    def _coalesce(self, times, ends, selected):
        previous = self._last()
        first = (
            previous is None
            or times[0] != previous[0]
            or tuple(c[0] for c in selected) != previous[1]
        )
        change = int.from_bytes(bytes(map(ne, ends[:-1], times[1:])), "little")
        for column in selected:
            value = int.from_bytes(column, "little")
            change |= value ^ (value >> 8)
        new = bytes([first]) + change.to_bytes(len(times), "little")[:-1].translate(_NONZERO)
        stops = bytearray(new[1:] + b"\x01")
        if not new[0]:
            stop = new.find(b"\x01")
            self.ends[-1] = ends[stop - 1] if stop >= 0 else ends[-1]
            stops[stop - 1 if stop >= 0 else -1] = 0
        self.times.extend(compress(times, new))
        self.ends.extend(compress(ends, stops))
        for c, column in zip(self.channels, selected, strict=True):
            self.columns[c].extend(compress(column, new))

    def _last(self):
        if not self.times:
            return None
        return self.ends[-1], tuple(self.columns[c][-1] for c in self.channels)

    def finish(self):
        self._flush(len(self.times))
        return self.rows


def _fresh(time, end, vector, previous):
    return time != end or vector != previous


def physical_rows(blocks, start, end, channels):
    """Build one day's rows from reader blocks clipped to ``[start, end)``."""
    builder = PhysicalBuilder(channels, start, end)
    for block in blocks:
        builder.add(block)
    return builder.finish()
