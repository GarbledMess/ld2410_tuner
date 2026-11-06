"""Identity-checked, bounded physical column sidecars for finished radar-days.

Unlike usable replay rows these retain missing readings on individual gates.
They are independent of configured gates, labels, and the request's cutoff.
"""

import json
import os
import struct
import sys
import tempfile
import zlib
from array import array
from bisect import bisect_left, bisect_right
from pathlib import Path

from ..engine.hybrid.constants import CELL_CACHE_DISK_BYTES, ROW_BLOCK_ROWS
from ..engine.hybrid.types import RowBlock
from . import summary_cache

_MAGIC = b"LD24COL3"
_HEADER = struct.Struct("<8sI")
_BLOCK = struct.Struct("<IIdd")
_SIZE = struct.Struct("<I")


def _read(stream, size):
    raw = stream.read(size)
    if len(raw) != size:
        raise ValueError("Truncated column cache")
    return raw


def _crc(stream, size):
    stream.seek(0)
    checksum = 0
    while size:
        raw = _read(stream, min(size, 65536))
        checksum = zlib.crc32(raw, checksum)
        size -= len(raw)
    return checksum


def _native(raw):
    result = array("d", raw)
    if sys.byteorder != "little":
        result.byteswap()
    return result


def _little(values):
    if sys.byteorder == "little":
        return values.tobytes()
    values = array("d", values)
    values.byteswap()
    return values.tobytes()


def _encode(block):
    geometry = _little(block.times) + _little(block.ends)
    # Byte shuffle gives the same exact doubles with compact regular clocks.
    shuffled = b"".join(geometry[k::8] for k in range(8))
    return zlib.compress(shuffled + b"".join(block.columns), 1)


def _decode(raw, n):
    raw = zlib.decompress(raw)
    if len(raw) != 34 * n:
        raise ValueError("Invalid column cache shape")
    geometry = bytearray(16 * n)
    for k in range(8):
        geometry[k::8] = raw[k * 2 * n : (k + 1) * 2 * n]
    columns = tuple(raw[(16 + c) * n : (17 + c) * n] for c in range(18))
    return RowBlock(_native(geometry[: 8 * n]), _native(geometry[8 * n :]), columns)


def _clipped(block, lo, hi, channels):
    first, stop = bisect_right(block.ends, lo), bisect_left(block.times, hi)
    if stop <= first:
        return None
    times, ends = block.times[first:stop], block.ends[first:stop]
    times[0], ends[-1] = max(lo, times[0]), min(hi, ends[-1])
    missing = bytes([255]) * (stop - first)
    columns = tuple(block.columns[c][first:stop] if c in channels else missing for c in range(18))
    return RowBlock(times, ends, columns)


def _block_header(stream, finish):
    size, n, start, end = _BLOCK.unpack(_read(stream, _BLOCK.size))
    if not 0 < n <= ROW_BLOCK_ROWS or size > 80 * n + 8192:
        raise ValueError("Invalid column block")
    if stream.tell() + size > finish:
        raise ValueError("Truncated column block")
    return size, n, start, end


def _blocks(stream, finish, lo, hi, channels):
    while stream.tell() < finish:
        size, n, start, end = _block_header(stream, finish)
        if not (start < hi and end > lo):
            stream.seek(size, 1)
            continue
        block = _clipped(_decode(_read(stream, size), n), lo, hi, channels)
        if block is not None:
            yield block


def _open(path, identity):
    if summary_cache.stamp(path) != tuple(identity[:2]):
        return None
    stream = path.with_suffix(".cols").open("rb")
    try:
        size = stream.seek(0, 2)
        if not _HEADER.size + 4 <= size <= CELL_CACHE_DISK_BYTES:
            raise ValueError("Invalid column cache size")
        stream.seek(size - 4)
        expected = _SIZE.unpack(_read(stream, 4))[0]
        if _crc(stream, size - 4) != expected:
            raise ValueError("Corrupt column cache")
        stream.seek(0)
        magic, length = _HEADER.unpack(_read(stream, _HEADER.size))
        if magic != _MAGIC or length > 4096:
            raise ValueError("Invalid column header")
        if json.loads(_read(stream, length)) != list(identity[:2]):
            raise ValueError("Stale column cache")
        if summary_cache.stamp(path) != tuple(identity[:2]):
            raise ValueError("Changed column source")
        return stream, size - 4
    except BaseException:
        stream.close()
        raise


def load(path, identity, lo, hi, channels):
    """Return a checked stream, or a cache miss before yielding any rows."""
    summary_cache.worker_only()
    try:
        opened = _open(Path(path), identity)
    except (OSError, ValueError, struct.error, zlib.error):
        return None
    if opened is None:
        return None
    return _lease(*opened, lo, hi, channels)


def _lease(stream, finish, lo, hi, channels):
    with stream:
        yield from _blocks(stream, finish, lo, hi, channels)


def store(path, root, identity, blocks):
    """Stream canonical blocks to an atomic sidecar; cache errors never own history."""
    summary_cache.worker_only()
    path, temporary = Path(path), None
    try:
        with summary_cache._lock:
            with tempfile.NamedTemporaryFile(
                dir=path.parent, suffix=".colstmp", mode="w+b", delete=False
            ) as stream:
                temporary = Path(stream.name)
                _write(stream, identity, blocks)
                size = stream.tell()
            if size <= CELL_CACHE_DISK_BYTES and summary_cache.stamp(path) == tuple(identity[:2]):
                os.replace(temporary, path.with_suffix(".cols"))
                summary_cache.prune_after(root, path.with_suffix(".cols"))
    except (OSError, ValueError, struct.error):
        pass
    finally:
        if temporary is not None:
            summary_cache._remove(temporary)


def _write(stream, identity, blocks):
    encoded = json.dumps(identity[:2], separators=(",", ":")).encode()
    stream.write(_HEADER.pack(_MAGIC, len(encoded)))
    stream.write(encoded)
    for block in blocks:
        if not block.times:
            continue
        raw = _encode(block)
        stream.write(_BLOCK.pack(len(raw), len(block.times), block.times[0], block.ends[-1]))
        stream.write(raw)
        if stream.tell() > CELL_CACHE_DISK_BYTES:
            raise ValueError("Column cache exceeds budget")
    size = stream.tell()
    checksum = _crc(stream, size)
    stream.seek(size)
    stream.write(_SIZE.pack(checksum))
