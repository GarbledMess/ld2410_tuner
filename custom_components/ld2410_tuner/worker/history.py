"""Bound finished history before allocating numerical columns in a worker."""

import struct
import zlib

from ..history import dayfile

# 131072 rows at the legacy maximum (18 float32 gates and two float64
# timestamp columns) occupy 11 MiB. This bounds one valid full-rate day.
DAY_ROWS = 131072
DAY_BYTES = 12 * 1024 * 1024


class HistoryRejected(ValueError):
    """A history input is malformed or exceeds the worker job budget."""


def _inflate(data, maximum):
    inflater = zlib.decompressobj()
    raw = inflater.decompress(data[4:], maximum + 1)
    if len(raw) > maximum or not inflater.eof or inflater.unused_data:
        raise HistoryRejected("Incomplete, trailing or oversized history stream")
    return raw


def _header(raw, max_rows, max_guesses):
    version, start, end, rows, guesses, *flags = dayfile.HEADER.unpack_from(raw)
    if version != dayfile.VERSION or end < start or any(flag not in (0, 1) for flag in flags):
        raise HistoryRejected("Invalid history header")
    if rows > max_rows or guesses > max_guesses:
        raise HistoryRejected("History count budget exceeded")
    expected = dayfile.HEADER.size + rows * (8 + sum(4 if flag else 1 for flag in flags))
    if expected + guesses * dayfile.RUN.size != len(raw):
        raise HistoryRejected("Invalid history payload length")
    return start, end, rows, guesses, flags


def decode(
    data,
    *,
    max_rows=32768,
    max_guesses=4096,
    max_raw_bytes=4 * 1024 * 1024,
    max_compressed_bytes=4 * 1024 * 1024,
):
    """Decode exactly one capped stream; reject counts before column allocation.

    This is a per-file allocation bound. The caller must enforce aggregate job
    budgets and install process limits before reading any history.
    """
    for limit in (max_rows, max_guesses, max_raw_bytes, max_compressed_bytes):
        if type(limit) is not int or not 0 <= limit < 2**63 - 1:
            raise ValueError("History budgets must be nonnegative bounded integers")
    if len(data) > max_compressed_bytes or data[:4] != dayfile.MAGIC:
        raise HistoryRejected("Invalid or oversized compressed history")
    try:
        raw = _inflate(data, max_raw_bytes)
        start, end, rows, guesses, flags = _header(raw, max_rows, max_guesses)
        times, position = dayfile._times(raw, dayfile.HEADER.size, rows, start)
        ends, position = dayfile._times(raw, position, rows, start)
        columns, position = dayfile._columns(raw, position, rows, flags)
        runs, _ = dayfile._runs(raw, position, guesses, start)
    except HistoryRejected:
        raise
    except (zlib.error, ValueError, IndexError, struct.error) as error:
        raise HistoryRejected("Invalid or oversized worker history") from error
    return dayfile.Day(dayfile._seconds(start), dayfile._seconds(end), times, ends, columns, runs)


def _identity(found):
    return found.st_dev, found.st_ino, found.st_mtime_ns, found.st_size


def read(reference, *, max_compressed_bytes=4 * 1024 * 1024, **budgets):
    """Read a captured file only while both descriptor and path identities match.

    Revalidate store rewrite markers again before accepting a completed job;
    these checks alone cannot establish ownership of the whole learning view.
    """
    import os
    import stat

    if type(max_compressed_bytes) is not int or not 0 <= max_compressed_bytes < 2**63 - 1:
        raise ValueError("Compressed history budget must be a bounded nonnegative integer")
    with open(reference.path, "rb", opener=_nonblocking) as stream:
        found = os.fstat(stream.fileno())
        if not stat.S_ISREG(found.st_mode) or _identity(found) != reference.identity:
            raise HistoryRejected("History file identity changed")
        if found.st_size > max_compressed_bytes:
            raise HistoryRejected("Compressed history budget exceeded")
        data = stream.read(max_compressed_bytes + 1)
        if _identity(os.fstat(stream.fileno())) != reference.identity:
            raise HistoryRejected("History changed during reading")
    if _identity(reference.path.stat()) != reference.identity:
        raise HistoryRejected("History path was replaced")
    return decode(data, max_compressed_bytes=max_compressed_bytes, **budgets)


def _nonblocking(path, flags):
    import os

    return os.open(path, flags | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0))
