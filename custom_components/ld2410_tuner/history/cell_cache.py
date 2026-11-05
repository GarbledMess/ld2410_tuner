"""Dedicated binary physical-cell sidecars, with no resident history cache."""

import json
import os
import struct
import sys
import tempfile
import zlib
from array import array
from collections import OrderedDict
from operator import lt
from pathlib import Path

from ..engine.hybrid.constants import (
    CELL_CACHE_DISK_BYTES,
    CELL_CACHE_MEMORY_BYTES,
    CELL_CACHE_VERSION,
    CELL_SECONDS,
)
from ..engine.hybrid.digest import DayDigest, PartialCoverage, _pieces
from ..engine.hybrid.index import EnergyIndex, read_index, write_index
from ..engine.hybrid.physical_index import PhysicalRows
from . import summary_cache

_MAGIC = b"LD24CELL1"
_HEADER = struct.Struct("<9sBqIII")
_PIECE = struct.Struct("<Idd")
_SIZE = struct.Struct("<I")


class _IndexOffsets:
    """Byte-bounded validation metadata; every reuse still checks the whole CRC."""

    def __init__(self):
        self.entries, self.size = OrderedDict(), 0

    def skip(self, stream, n, key, checksum):
        key = key, checksum, n, stream.tell()
        if key in self.entries:
            self.entries.move_to_end(key)
            stream.seek(self.entries[key][0])
            return
        _skip_index(stream, n)
        self._remember(key, stream.tell())

    def _remember(self, key, offset):
        size = 1024 + len(str(key[0]).encode())
        if key[0] is None or size > CELL_CACHE_MEMORY_BYTES:
            return
        while self.entries and self.size + size > CELL_CACHE_MEMORY_BYTES:
            _, (_, removed) = self.entries.popitem(last=False)
            self.size -= removed
        self.entries[key] = offset, size
        self.size += size


_INDEX_OFFSETS = _IndexOffsets()


def _index_key(path, found):
    return str(path), found.st_dev, found.st_ino, found.st_mtime_ns, found.st_size


def _identity(path, identity):
    return summary_cache.stamp(path) == tuple(identity[:2])


def _read(stream, size):
    raw = stream.read(size)
    if len(raw) != size:
        raise ValueError("Truncated cell cache")
    return raw


def _checksum(stream, size):
    stream.seek(0)
    crc = 0
    while size:
        chunk = _read(stream, min(65536, size))
        crc = zlib.crc32(chunk, crc)
        size -= len(chunk)
    return crc


def _mask(channels):
    return sum(1 << c for c in set(channels))


def _header(stream, identity, channels):
    magic, version, k0, n, mask, size = _HEADER.unpack(_read(stream, _HEADER.size))
    if magic != _MAGIC or version != CELL_CACHE_VERSION or mask != _mask(channels):
        raise ValueError("Stale cell cache")
    if not 0 < n <= 14401 or size > 4096:
        raise ValueError("Invalid cell shape")
    if json.loads(_read(stream, size)) != json.loads(json.dumps(identity[:2])):
        raise ValueError("Stale cell source")
    return k0, n


def _validate_piece(cell, lo, hi, cov, k0, previous):
    if cell >= len(cov) or cov[cell] != 2:
        raise ValueError("Invalid partial cell")
    edge = (k0 + cell) * CELL_SECONDS
    if not edge <= lo < hi <= edge + CELL_SECONDS:
        raise ValueError("Invalid partial geometry")
    if cell < previous[0] or (cell == previous[0] and lo <= previous[1]):
        raise ValueError("Unordered partial geometry")


def _partial(stream, finish, cov, k0):
    partial = PartialCoverage()
    previous = (-1, float("-inf"))
    try:
        while stream.tell() < finish:
            cell, lo, hi = _PIECE.unpack(_read(stream, _PIECE.size))
            _validate_piece(cell, lo, hi, cov, k0, previous)
            partial.append(cell, lo, hi)
            previous = cell, hi
        if set(partial) != {i for i, value in enumerate(cov) if value == 2}:
            raise ValueError("Missing partial geometry")
        return partial
    except BaseException:
        partial.close()
        raise


def _load(stream, size, identity, channels, *, with_index=True, index_key=None):
    expected = struct.unpack("<I", _read_at(stream, size - 4, 4))[0]
    if _checksum(stream, size - 4) != expected:
        raise ValueError("Corrupt cell checksum")
    stream.seek(0)
    k0, n = _header(stream, identity, channels)
    columns = tuple(_read(stream, n) for _ in range(18))
    cov = _read(stream, n)
    if any(v > 2 for v in cov):
        raise ValueError("Invalid cell coverage")
    index = (
        read_index(stream, n) if with_index else _INDEX_OFFSETS.skip(stream, n, index_key, expected)
    )
    physical = _physical(stream, size - 4, channels)
    partial = _partial(stream, size - 4, cov, k0)
    return DayDigest(CELL_SECONDS, k0, n, columns, cov, partial, index, physical)


def _load_physical(stream, size, identity, channels, *, index_key=None):
    """Read replay rows without rebuilding the unrelated energy-position index."""
    expected = struct.unpack("<I", _read_at(stream, size - 4, 4))[0]
    if _checksum(stream, size - 4) != expected:
        raise ValueError("Corrupt cell checksum")
    stream.seek(0)
    _, n = _header(stream, identity, channels)
    offset = stream.tell() + 19 * n  # 18 energy columns and coverage.
    if offset > size - 4:
        raise ValueError("Truncated cell cache")
    stream.seek(offset)
    _INDEX_OFFSETS.skip(stream, n, index_key, expected)
    return _physical(stream, size - 4, channels)


def _skip_index(stream, n):
    """Validate unused positions without retaining an energy-position index."""
    if _read(stream, 1)[0] != 18:
        raise ValueError("Invalid energy index channels")
    for _ in range(18):
        _skip_column(stream, n)


def _skip_column(stream, n):
    buckets = _read(stream, 1)[0]
    if buckets > 101:
        raise ValueError("Invalid energy buckets")
    seen = 0
    for _ in range(buckets):
        energy, count = struct.unpack("<BH", _read(stream, 3))
        if energy > 100 or count > n or seen & (1 << energy):
            raise ValueError("Invalid energy positions")
        seen |= 1 << energy
        _check_positions(_read(stream, 2 * count), n)


def _check_positions(raw, n):
    positions = array("H", raw)
    if sys.byteorder != "little":
        positions.byteswap()
    if positions and (positions[-1] >= n or not all(map(lt, positions, positions[1:]))):
        raise ValueError("Invalid energy positions")


def _physical(stream, finish, channels):
    """Physical rows stay compressed until exact replay or a row budget reads them."""
    length = _SIZE.unpack(_read(stream, _SIZE.size))[0]
    if stream.tell() + length > finish:
        raise ValueError("Invalid physical rows size")
    return PhysicalRows.read(stream, length, channels)


def _read_at(stream, offset, size):
    stream.seek(offset)
    return _read(stream, size)


def load(path, identity, channels, *, with_index=True):
    """An auxiliary error is always a miss; physical identity must remain current."""
    summary_cache.worker_only()
    path = Path(path)
    result = None
    try:
        if not _identity(path, identity):
            return None
        cache = path.with_suffix(".cell")
        found = cache.stat()
        size = found.st_size
        if not _HEADER.size + 4 <= size <= CELL_CACHE_DISK_BYTES:
            return None
        with cache.open("rb") as stream:
            result = _load(
                stream,
                size,
                identity,
                channels,
                with_index=with_index,
                index_key=_index_key(cache, found),
            )
        if _identity(path, identity):
            return result
    except (OSError, ValueError, struct.error):
        pass
    if result is not None:
        result.partial.close()
    return None


def load_physical(path, identity, channels):
    """Load only the CRC-checked physical section for exact row replay."""
    summary_cache.worker_only()
    path = Path(path)
    try:
        if not _identity(path, identity):
            return None
        cache = path.with_suffix(".cell")
        found = cache.stat()
        size = found.st_size
        if not _HEADER.size + 4 <= size <= CELL_CACHE_DISK_BYTES:
            return None
        with cache.open("rb") as stream:
            result = _load_physical(
                stream, size, identity, channels, index_key=_index_key(cache, found)
            )
        if _identity(path, identity):
            return result
    except (OSError, ValueError, struct.error):
        pass
    return None


def _write(stream, identity, channels, digest):
    encoded = json.dumps(identity[:2], separators=(",", ":")).encode()
    stream.write(
        _HEADER.pack(_MAGIC, CELL_CACHE_VERSION, digest.k0, digest.n, _mask(channels), len(encoded))
    )
    stream.write(encoded)
    for column in digest.channels:
        stream.write(column)
    stream.write(digest.cov)
    write_index(stream, digest.index or EnergyIndex.build(digest.channels))
    if digest.physical is None:
        raise ValueError("Digest has no physical rows")
    stream.write(_SIZE.pack(digest.physical.encoded_size()))
    digest.physical.write(stream)
    for cell in digest.partial:
        for lo, hi in _pieces(digest.partial, cell):
            stream.write(_PIECE.pack(cell, lo, hi))
    size = stream.tell()
    crc = _checksum(stream, size)
    stream.seek(size)
    stream.write(struct.pack("<I", crc))


def store(path, root, identity, channels, digest):
    """Publish atomically only for the captured finished source."""
    summary_cache.worker_only()
    path = Path(path)
    temporary = None
    try:
        with summary_cache._lock:
            if not _identity(path, identity):
                return
            with tempfile.NamedTemporaryFile(
                dir=path.parent, suffix=".celltmp", mode="w+b", delete=False
            ) as stream:
                temporary = Path(stream.name)
                _write(stream, identity, channels, digest)
                size = stream.tell()
            if size <= CELL_CACHE_DISK_BYTES and _identity(path, identity):
                os.replace(temporary, path.with_suffix(".cell"))
            summary_cache.prune(root)
    except (OSError, ValueError, struct.error):
        pass
    finally:
        if temporary is not None:
            summary_cache._remove(temporary)
