"""Bound finished history before allocating numerical columns in a worker."""

import io
import mmap
import struct
import tempfile
import zlib
from contextlib import contextmanager

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
    if version != dayfile.VERSION or end < start or any(flag not in (0, 1) for flag in flags):
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
    return start, end, rows, guesses, flags


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
