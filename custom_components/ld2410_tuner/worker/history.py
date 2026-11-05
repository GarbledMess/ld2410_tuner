"""Bound finished history before allocating numerical columns in a worker."""

import io
import mmap
import struct
import sys
import tempfile
import zlib
from array import array
from contextlib import contextmanager
from itertools import repeat
from operator import add, lt, truediv

from ..history import dayfile

# Reserve at most a quarter of the mandatory 512 MiB address-space allowance
# for a single decoded representation; mapped wire bytes use the same ceiling.
# Count limits are optional caller policy, never a sampling-rate assumption.
DAY_BYTES = 128 * 1024 * 1024
DAY_ROWS = None
GUESS_BYTES = 256  # tuple, list slot and independently allocated Python numbers.


class HistoryRejected(ValueError):
    """A history input is malformed or exceeds the worker job budget."""


@contextmanager
def _inflate(stream, maximum):
    inflater = zlib.decompressobj()
    total = 0
    with tempfile.TemporaryFile() as output:
        while chunk := stream.read(65536):
            while chunk:
                raw = inflater.decompress(chunk, min(65536, maximum - total + 1))
                total += len(raw)
                if total > maximum or inflater.unused_data:
                    raise HistoryRejected("Incomplete, trailing or oversized history stream")
                output.write(raw)
                chunk = inflater.unconsumed_tail
        if not inflater.eof or total < dayfile.HEADER.size:
            raise HistoryRejected("Incomplete, trailing or oversized history stream")
        output.flush()
        with mmap.mmap(output.fileno(), 0, access=mmap.ACCESS_READ) as raw:
            yield raw


def _header(raw, max_rows, max_guesses):
    version, start, end, rows, guesses, *flags = dayfile.HEADER.unpack_from(raw)
    if version not in dayfile.VERSIONS or end < start or any(flag not in (0, 1) for flag in flags):
        raise HistoryRejected("Invalid history header")
    if (max_rows is not None and rows > max_rows) or (
        max_guesses is not None and guesses > max_guesses
    ):
        raise HistoryRejected("History count budget exceeded")
    expected = dayfile.HEADER.size + rows * (8 + sum(4 if flag else 1 for flag in flags))
    if expected + guesses * dayfile.RUN.size != len(raw):
        raise HistoryRejected("Invalid history payload length")
    resident = rows * (16 + sum(4 if flag else 1 for flag in flags))
    if resident + guesses * GUESS_BYTES > DAY_BYTES:
        raise HistoryRejected("Decoded history representation exceeds worker memory allowance")
    return version, start, end, rows, guesses, flags


def decode(
    data,
    *,
    max_rows=None,
    max_guesses=None,
    max_raw_bytes=DAY_BYTES,
    max_compressed_bytes=None,
):
    """Spill wire bytes and bound the decoded representation before allocation.

    Counts and compressed size are optional caller policies. The mandatory
    process limits must be installed before reading any history.
    """
    stream = _stream(data, (max_rows, max_guesses, max_raw_bytes, max_compressed_bytes))
    try:
        with _inflate(
            stream, DAY_BYTES if max_raw_bytes is None else min(DAY_BYTES, max_raw_bytes)
        ) as raw:
            version, start, end, rows, guesses, flags = _header(raw, max_rows, max_guesses)
            times, ends, position = dayfile._time_columns(
                raw, dayfile.HEADER.size, rows, start, end, version
            )
            columns, position = dayfile._columns(raw, position, rows, flags)
            runs, _ = dayfile._runs(raw, position, guesses, start)
    except HistoryRejected:
        raise
    except (zlib.error, ValueError, IndexError, struct.error) as error:
        raise HistoryRejected("Invalid or oversized worker history") from error
    return dayfile.Day(dayfile._seconds(start), dayfile._seconds(end), times, ends, columns, runs)


def _stream(data, budgets):
    max_compressed_bytes = budgets[-1]
    for limit in budgets:
        if limit is not None and (type(limit) is not int or not 0 <= limit < 2**63 - 1):
            raise ValueError("History budgets must be nonnegative bounded integers")
    stream = io.BytesIO(data) if isinstance(data, (bytes, bytearray)) else data
    if isinstance(data, (bytes, bytearray)) and (
        max_compressed_bytes is not None and len(data) > max_compressed_bytes
    ):
        raise HistoryRejected("Invalid or oversized compressed history")
    if stream.read(4) != dayfile.MAGIC:
        raise HistoryRejected("Invalid or oversized compressed history")
    return stream


def _identity(found):
    return found.st_dev, found.st_ino, found.st_mtime_ns, found.st_size


def read(reference, *, max_compressed_bytes=None, **budgets):
    """Read a captured file only while both descriptor and path identities match.

    Revalidate store rewrite markers again before accepting a completed job;
    these checks alone cannot establish ownership of the whole learning view.
    """
    import os
    import stat

    if max_compressed_bytes is not None and (
        type(max_compressed_bytes) is not int or not 0 <= max_compressed_bytes < 2**63 - 1
    ):
        raise ValueError("Compressed history budget must be a bounded nonnegative integer")
    with open(reference.path, "rb", opener=_nonblocking) as stream:
        found = os.fstat(stream.fileno())
        if not stat.S_ISREG(found.st_mode) or _identity(found) != reference.identity:
            raise HistoryRejected("History file identity changed")
        if max_compressed_bytes is not None and found.st_size > max_compressed_bytes:
            raise HistoryRejected("Compressed history budget exceeded")
        day = decode(stream, max_compressed_bytes=max_compressed_bytes, **budgets)
        if _identity(os.fstat(stream.fileno())) != reference.identity:
            raise HistoryRejected("History changed during reading")
    if _identity(reference.path.stat()) != reference.identity:
        raise HistoryRejected("History path was replaced")
    return day


def _nonblocking(path, flags):
    import os

    return os.open(path, flags | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0))


def _read_at(stream, offset, size):
    stream.seek(offset)
    data = stream.read(size)
    if len(data) != size:
        raise HistoryRejected("Truncated physical columns")
    return data


@contextmanager
def _spool_columns(stream):
    """Inflate bounded buffers to disk; never map the complete payload."""
    if stream.read(4) != dayfile.MAGIC:
        raise HistoryRejected("Invalid physical stream")
    inflater = zlib.decompressobj()
    with tempfile.TemporaryFile() as raw:
        while chunk := stream.read(65536):
            while chunk:
                data = inflater.decompress(chunk, 65536)
                raw.write(data)
                if inflater.unused_data:
                    raise HistoryRejected("Trailing physical stream")
                chunk = inflater.unconsumed_tail
        if not inflater.eof:
            raise HistoryRejected("Incomplete physical stream")
        size = raw.tell()
        header = dayfile.HEADER.unpack(_read_at(raw, 0, dayfile.HEADER.size))
        _column_header(header, size)
        yield raw, header


def _column_header(header, size):
    version, start, end, rows, runs, *flags = header
    if version not in dayfile.VERSIONS or end < start or any(f not in (0, 1) for f in flags):
        raise HistoryRejected("Invalid physical header")
    expected = dayfile.HEADER.size + rows * (8 + sum(4 if f else 1 for f in flags))
    if expected + runs * dayfile.RUN.size != size:
        raise HistoryRejected("Invalid physical payload length")


def _integers(stream, offset, count, first, length, planes):
    if not planes:
        raw = _read_at(stream, offset + first * 4, length * 4)
    else:
        raw = bytearray(length * 4)
        for plane in range(4):
            raw[plane::4] = _read_at(stream, offset + plane * count + first, length)
    values = array("i", raw)
    if sys.byteorder != "little":
        values.byteswap()
    return values.tolist()


class _ColumnReader:
    """Disk-backed timestamps support bisection and bounded row-range decoding."""

    def __init__(self, raw, header, index):
        self.raw, self.index = raw, index
        self.version, self.start, self.end, self.count, _, *self.flags = header
        self.offsets = []
        offset = dayfile.HEADER.size + self.count * 8
        for flag in self.flags:
            self.offsets.append(offset)
            offset += self.count * (4 if flag else 1)
        self._index_times()

    def _time_block(self, first, length, previous):
        from itertools import accumulate

        base = dayfile.HEADER.size
        steps = _integers(self.raw, base, self.count, first, length, self.version == 3)
        second = _integers(
            self.raw, base + self.count * 4, self.count, first, length, self.version == 3
        )
        if self.version == 1:
            return steps, second
        if min(steps, default=0) < 0:
            raise HistoryRejected("Invalid delta timestamps")
        starts = list(accumulate(steps, initial=previous))[1:]
        following = starts
        if self.version == 3:
            last = starts[-1]
            if first + length < self.count:
                last += _integers(self.raw, base, self.count, first + length, 1, True)[0]
            following = starts[1:] + [last]
        return starts, [a + b for a, b in zip(following, second, strict=True)]

    def _index_times(self):
        from ..engine.hybrid.constants import ROW_BLOCK_ROWS
        from ..history import pace

        previous, previous_end = 0, None
        for first in range(0, self.count, ROW_BLOCK_ROWS):
            length = min(ROW_BLOCK_ROWS, self.count - first)
            starts, ends = self._time_block(first, length, previous)
            previous_end = self._write_times(starts, ends, previous_end)
            previous = starts[-1]
            pace.rest()

    def _write_times(self, starts, ends, previous_end):
        if len(starts) != len(ends):
            raise HistoryRejected("Invalid physical coverage")
        if any(map(lt, ends, starts)) or max(ends, default=0) > self.end - self.start:
            raise HistoryRejected("Invalid physical coverage")
        if starts and previous_end is not None and starts[0] < previous_end:
            raise HistoryRejected("Unordered physical coverage")
        if any(map(lt, starts[1:], ends[:-1])):
            raise HistoryRejected("Unordered physical coverage")
        times = map(truediv, map(add, starts, repeat(self.start)), repeat(1000))
        stops = map(truediv, map(add, ends, repeat(self.start)), repeat(1000))
        values = array("d", [0.0]) * (2 * len(starts))
        values[::2] = array("d", times)
        values[1::2] = array("d", stops)
        if sys.byteorder != "little":
            values.byteswap()
        self.index.write(values.tobytes())
        return ends[-1] if ends else previous_end

    def _bisect(self, value, side):
        lo, hi = 0, self.count
        while lo < hi:
            mid = (lo + hi) // 2
            pair = struct.unpack("<dd", _read_at(self.index, mid * 16, 16))
            advance = pair[1] <= value if side == "end" else pair[0] < value
            if advance:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def rows_in(self, lo, hi):
        if hi <= lo:
            return 0
        return max(0, self._bisect(hi, "start") - self._bisect(lo, "end"))

    def _column(self, channel, first, count):
        from math import ceil, isfinite

        width = 4 if self.flags[channel] else 1
        data = _read_at(self.raw, self.offsets[channel] + first * width, count * width)
        if width == 1:
            return data
        values = (v[0] for v in struct.iter_unpack("<f", data))
        return bytes(ceil(v) if isfinite(v) and 0 <= v <= 100 else 255 for v in values)

    def blocks(self, lo, hi, channels):
        from ..engine.hybrid.constants import ROW_BLOCK_ROWS
        from ..engine.hybrid.types import RowBlock
        from ..history import pace

        if hi <= lo:
            return
        first, after = self._bisect(lo, "end"), self._bisect(hi, "start")
        for begin in range(first, after, ROW_BLOCK_ROWS):
            count = min(ROW_BLOCK_ROWS, after - begin)
            pairs = array("d", _read_at(self.index, begin * 16, count * 16))
            if sys.byteorder != "little":
                pairs.byteswap()
            times, ends = pairs[::2], pairs[1::2]
            times[0], ends[-1] = max(lo, times[0]), min(hi, ends[-1])
            columns = tuple(
                self._column(c, begin, count) if c in channels else bytes([255]) * count
                for c in range(18)
            )
            yield RowBlock(times, ends, columns)
            pace.rest()


def _check_reference(stream, reference):
    import os
    import stat

    found = os.fstat(stream.fileno())
    if not stat.S_ISREG(found.st_mode) or _identity(found) != reference.identity:
        raise HistoryRejected("History file identity changed")
    if _identity(reference.path.stat()) != reference.identity:
        raise HistoryRejected("History path was replaced")


def guess_runs(reference):
    """Decode captured guess records in bounded blocks without indexing readings."""
    try:
        with open(reference.path, "rb", opener=_nonblocking) as stream:
            _check_reference(stream, reference)
            try:
                with _spool_columns(stream) as (raw, header):
                    yield from _guess_records(raw, header)
            finally:
                _check_reference(stream, reference)
    except (zlib.error, struct.error, IndexError) as error:
        raise HistoryRejected("Malformed guess history") from error


def _guess_records(raw, header):
    from ..engine.hybrid.constants import ROW_BLOCK_ROWS
    from ..history import pace

    _, start, _, rows, count, *flags = header
    offset = dayfile.HEADER.size + rows * (8 + sum(4 if f else 1 for f in flags))
    for first in range(0, count, ROW_BLOCK_ROWS):
        length = min(ROW_BLOCK_ROWS, count - first)
        data = _read_at(raw, offset + first * dayfile.RUN.size, length * dayfile.RUN.size)
        for lo, hi, state, level, basis in dayfile.RUN.iter_unpack(data):
            yield (start + lo) / 1000, (start + hi) / 1000, dayfile.STATES[state], level, basis
        pace.rest()


@contextmanager
def open_columns(reference):
    """Hold a captured descriptor and validate identity on entry and every exit."""
    try:
        with open(reference.path, "rb", opener=_nonblocking) as stream:
            _check_reference(stream, reference)
            with _spool_columns(stream) as (raw, header), tempfile.TemporaryFile() as index:
                reader = _ColumnReader(raw, header, index)
                try:
                    yield reader
                finally:
                    _check_reference(stream, reference)
    except (zlib.error, struct.error) as error:
        raise HistoryRejected("Malformed physical history") from error
