"""Optional worker-only physical indexes; corruption is always a cache miss.

Compressed auxiliary files have their own disk budget and never own physical history.
The portable binary format contains bounded numeric arrays, never executable objects.
"""

import asyncio
import io
import os
import struct
import sys
import tempfile
import threading
import zlib
from array import array
from math import isfinite

from . import summaries

MAX_DISK_BYTES = 64 * 1024 * 1024
MAX_RAW_BYTES = 64 * 1024 * 1024
_MAGIC = b"LD24SUM1"
_HEADER = struct.Struct("<8sQQQdd")
_LENGTH = struct.Struct("<I")
_lock = threading.RLock()


def worker_only():
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError("Physical summary disk access requires an executor")


def stamp(path):
    found = path.stat()
    return found.st_mtime_ns, found.st_size


def _put(out, values):
    if isinstance(values, array):
        values = array(values.typecode, values)
        if sys.byteorder != "little":
            values.byteswap()
        data = values.tobytes()
    else:
        data = bytes(values)
    if out.tell() + 4 + len(data) > MAX_RAW_BYTES:
        raise ValueError("Summary exceeds decode budget")
    out.write(_LENGTH.pack(len(data)))
    out.write(data)


def _encode(summary, identity):
    day = summary.day
    out = io.BytesIO()
    out.write(_HEADER.pack(_MAGIC, *identity, len(day.times), day.start, day.end))
    for part in (
        summary.prefix,
        summary.coverage,
        summary.known,
        summary.coverage_starts,
        summary.coverage_ends,
    ):
        _put(out, part)
    out.write(_LENGTH.pack(len(summary.gates)))
    for bins in summary.gates:
        out.write(_LENGTH.pack(len(bins)))
        for energy, stream in bins.items():
            out.write(_LENGTH.pack(energy))
            for part in (stream.starts, stream.ends, stream.mass):
                _put(out, part)
    return zlib.compress(out.getvalue(), level=1)


class _Reader:
    def __init__(self, raw):
        self.raw, self.offset = memoryview(raw), _HEADER.size

    def number(self):
        if self.offset + 4 > len(self.raw):
            raise ValueError("Truncated summary")
        value = _LENGTH.unpack_from(self.raw, self.offset)[0]
        self.offset += 4
        return value

    def part(self, code, maximum):
        size = self.number()
        width = 1 if code is None else array(code).itemsize
        if size % width or size // width > maximum or self.offset + size > len(self.raw):
            raise ValueError("Invalid summary array")
        data = self.raw[self.offset : self.offset + size]
        self.offset += size
        if code is None:
            return bytearray(data)
        found = array(code)
        found.frombytes(data)
        if sys.byteorder != "little":
            found.byteswap()
        return found


def _ordered(values):
    return all(isfinite(value) for value in values) and all(
        a <= b for a, b in zip(values, values[1:], strict=False)
    )


def _decode(raw, day, identity):
    magic, modified, size, rows, start, end = _HEADER.unpack_from(raw)
    if (
        magic != _MAGIC
        or (modified, size) != identity
        or rows != len(day.times)
        or (start, end) != (day.start, day.end)
    ):
        raise ValueError("Stale summary")
    reader = _Reader(raw)
    result = summaries.Summary.__new__(summaries.Summary)
    result.day = day
    _read_coverage(reader, result, rows)
    result.gates = _read_gates(reader, day)
    if reader.offset != len(raw) or result.nbytes > MAX_RAW_BYTES:
        raise ValueError("Invalid summary length")
    return result


def _read_coverage(reader, result, rows):
    result.prefix = reader.part("d", rows + 1)
    result.coverage = reader.part("d", rows + 1)
    result.known = reader.part(None, rows)
    result.coverage_starts = reader.part("d", rows)
    result.coverage_ends = reader.part("d", rows)
    if (
        len(result.prefix) != rows + 1
        or len(result.coverage) != rows + 1
        or len(result.known) != rows
        or not _ordered(result.prefix)
        or not _ordered(result.coverage)
        or result.prefix[0] != 0
        or result.coverage[0] != 0
        or any(value > 1 for value in result.known)
        or len(result.coverage_starts) != len(result.coverage_ends)
        or not _ordered(result.coverage_starts)
        or not _ordered(result.coverage_ends)
    ):
        raise ValueError("Invalid summary coverage")


def _read_stream(reader, rows):
    stream = summaries._Bin()
    stream.starts = reader.part("I", rows)
    stream.ends = reader.part("I", rows)
    stream.mass = reader.part("d", rows + 1)
    if (
        len(stream.starts) != len(stream.ends)
        or len(stream.mass) != len(stream.starts) + 1
        or not _ordered(stream.starts)
        or not _ordered(stream.ends)
        or not _ordered(stream.mass)
        or stream.mass[0] != 0
        or any(a >= b or b > rows for a, b in zip(stream.starts, stream.ends, strict=True))
    ):
        raise ValueError("Invalid energy stream")
    return stream


def _read_gates(reader, day):
    if reader.number() != len(day.columns):
        raise ValueError("Invalid summary gates")
    gates = []
    for _ in day.columns:
        bins = {}
        count = reader.number()
        if count > 101:
            raise ValueError("Invalid summary bins")
        for _ in range(count):
            energy = reader.number()
            if energy > 100 or energy in bins:
                raise ValueError("Invalid energy")
            bins[energy] = _read_stream(reader, len(day.times))
        gates.append(bins)
    return gates


def load(day, path, *, identity=None):
    worker_only()
    try:
        if identity is None:
            identity = stamp(path)
        elif stamp(path) != identity:
            return None
        cached = path.with_suffix(".sum")
        with cached.open("rb") as source:
            compressed = source.read(MAX_DISK_BYTES + 1)
        if len(compressed) > MAX_DISK_BYTES:
            return None
        decoder = zlib.decompressobj()
        raw = decoder.decompress(compressed, MAX_RAW_BYTES + 1)
        if len(raw) > MAX_RAW_BYTES or not decoder.eof or decoder.unused_data:
            return None
        result = _decode(raw, day, identity)
        if stamp(path) != identity:
            return None
        os.utime(cached, None)  # Worker-only LRU touch; never label-dependent.
        return result
    except (OSError, ValueError, OverflowError, struct.error, zlib.error):
        return None


def _remove(path):
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _cache_files(root):
    files = []
    for path in root.rglob("*.sum"):
        try:
            found = path.stat()
            files.append((found.st_mtime_ns, path, found.st_size))
        except OSError:
            continue
    return files


def disk_bytes(root):
    worker_only()
    return sum(size for _, _, size in _cache_files(root))


def prune(root, *, max_bytes=None):
    worker_only()
    limit = MAX_DISK_BYTES if max_bytes is None else max_bytes
    with _lock:
        # Writers hold this lock while a temporary exists, so these are crash leftovers.
        for temporary in root.rglob("*.sumtmp"):
            _remove(temporary)
        files = _cache_files(root)
        total = sum(size for _, _, size in files)
        for _, path, size in sorted(files):
            if total <= limit:
                break
            if _remove(path):
                total -= size


def store(summary, path, root, *, identity=None):
    worker_only()
    temporary = None
    try:
        identity = stamp(path) if identity is None else identity
        encoded = _encode(summary, identity)
        if len(encoded) > MAX_DISK_BYTES:
            prune(root)
            return
        with _lock:
            with tempfile.NamedTemporaryFile(
                dir=path.parent, suffix=".sumtmp", delete=False
            ) as out:
                temporary = out.name
                out.write(encoded)
            if stamp(path) != identity:
                return
            os.replace(temporary, path.with_suffix(".sum"))
            temporary = None
            prune(root)
    except (OSError, ValueError, OverflowError, struct.error):
        pass  # Auxiliary storage must never prevent history queries.
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def invalidate(path):
    """Remove an optional sidecar alongside an already-authorized history mutation."""
    try:
        path.with_suffix(".sum").unlink(missing_ok=True)
    except OSError:
        pass  # File identity still prevents reuse after mutation.
