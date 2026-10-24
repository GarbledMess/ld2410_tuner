"""Bounded worker-only derived results beside immutable finished days."""

import json
import os
import tempfile
import zlib
from array import array
from collections import OrderedDict
from functools import lru_cache
from hashlib import blake2b
from math import isfinite
from pathlib import Path

from . import summary_cache

MAX_FILE_BYTES = 4 * 1024 * 1024
MAX_RAW_BYTES = 16 * 1024 * 1024
MAX_VALUE_BYTES = 1024 * 1024
MAX_ENTRIES = 256
MAX_DISK_BYTES = 32 * 1024 * 1024  # All results, apart from the physical indexes' budget.
NAMES = frozenset(("rows", "chunks", "quality"))
MEMORY = 64  # Days' result files kept parsed in this process.
PRUNE_EVERY = 64
_files = OrderedDict()  # Sidecar path -> (its stamp, day identity, producer, entries).
_writes = 0


@lru_cache(maxsize=1)
def _producer():
    root = Path(__file__).resolve().parent.parent
    digest = blake2b(digest_size=16)
    for name in (
        "calibration/evidence_plan.py",
        "calibration/evidence_budget.py",
        "calibration/summary_quality.py",
        "history/summaries.py",
        "history/dayfile.py",
        "history/day_results.py",
        "history/result_cache.py",
    ):
        digest.update((root / name).read_bytes())
    return digest.hexdigest()


def _key(name, args):
    return blake2b(repr((name, args)).encode(), digest_size=16).hexdigest()


def _number(value):
    return type(value) in (int, float) and isfinite(value)


def _require(condition):
    if not condition:
        raise ValueError("Invalid derived result")


def _chunk(value):
    from ..calibration.evidence_budget import Chunk

    _require(type(value) is Chunk)
    _require(all(_number(v) for v in (value.start, value.end, value.seconds)))
    _require(type(value.rows) is int and value.rows >= 0)
    _require(type(value.edge) is bool)
    _require(all(v is None or _number(v) for v in (value.peak, value.floor)))
    state = value.state
    _require(
        state is None
        or type(state) is str
        or (type(state) is tuple and all(type(v) is str for v in state))
    )


def _histogram(value):
    _require(type(value) is dict)
    for gate, bins in value.items():
        _require(type(gate) is int and 0 <= gate < 18)
        _require(type(bins) is array and bins.typecode == "d")
        _require(all(isfinite(v) and v >= 0 for v in bins))


def _quality(value):
    _require(type(value) is tuple and len(value) == 3)
    physical, histograms, valid = value
    _require(type(physical) is bytes)
    _require(type(histograms) is tuple and type(valid) is tuple)
    for histogram in histograms:
        _histogram(histogram)
    for span in valid:
        _require(type(span) is tuple and len(span) == 2)
        _require(all(type(v) is float and isfinite(v) for v in span))
        _require(span[0] <= span[1])


def _validate(name, value):
    _require(name in NAMES)
    if name == "rows":
        _require(type(value) is int and value >= 0)
    elif name == "quality":
        _quality(value)
    elif value is not None:
        _require(type(value) is tuple)
        for chunk in value:
            _chunk(chunk)


def _pack(value):
    if isinstance(value, tuple):
        tag = "chunk" if hasattr(value, "_fields") else "tuple"
        return [tag, [_pack(v) for v in value]]
    if type(value) is dict:
        return ["dict", [[k, _pack(v)] for k, v in value.items()]]
    if type(value) is array:
        return ["array", list(value)]
    if type(value) is bytes:
        return ["bytes", value.hex()]
    return value


def _dictionary(items):
    result = {}
    for key, value in items:
        _require(type(key) is int and key not in result)
        result[key] = _unpack(value)
    return result


def _unpack(value):
    if type(value) is not list:
        _require(value is None or type(value) in (str, int, float, bool))
        return value
    _require(len(value) == 2)
    tag, items = value
    if tag == "bytes":
        _require(type(items) is str)
        return bytes.fromhex(items)
    _require(type(items) is list)
    if tag == "array":
        _require(all(type(v) is float and isfinite(v) for v in items))
        return array("d", items)
    if tag == "dict":
        return _dictionary(items)
    _require(tag in ("tuple", "chunk"))
    values = tuple(_unpack(v) for v in items)
    if tag == "chunk":
        from ..calibration.evidence_budget import Chunk

        return Chunk(*values)
    return values


def _json(value):
    return json.dumps(value, allow_nan=False, separators=(",", ":")).encode()


def _object(items):
    result = {}
    for key, value in items:
        _require(key not in result)
        result[key] = value
    return result


def _valid_key(key):
    return len(key) == 32 and all(char in "0123456789abcdef" for char in key)


def _read(path, identity):
    with path.with_suffix(".res").open("rb") as source:
        compressed = source.read(MAX_FILE_BYTES + 1)
    _require(len(compressed) <= MAX_FILE_BYTES)
    decoder = zlib.decompressobj()
    raw = decoder.decompress(compressed, MAX_RAW_BYTES + 1)
    _require(len(raw) <= MAX_RAW_BYTES and decoder.eof and not decoder.unused_data)
    data = json.loads(raw, object_pairs_hook=_object)
    _require(type(data) is dict and data.get("magic") == "LD24RES1")
    _require(set(data) == {"magic", "identity", "producer", "entries"})
    _require(_json(data["identity"]) == _json(identity) and data["producer"] == _producer())
    entries = data["entries"]
    _require(type(entries) is dict and len(entries) <= MAX_ENTRIES)
    _require(all(_valid_key(k) for k in entries))
    return entries


def _current(path, identity):
    return summary_cache.stamp(path) == tuple(identity[:2])


def _entries(path, identity):
    """The day's stored entries, read from disk once per process while its file stays as it
    was (its own size and time, the day's identity and the producing code)."""
    sidecar = path.with_suffix(".res")
    stamp = summary_cache.stamp(sidecar)
    found = _files.get(sidecar)
    if found is not None and found[:3] == (stamp, _json(identity), _producer()):
        _files.move_to_end(sidecar)
        return found[3]
    entries = _read(path, identity)
    os.utime(sidecar, None)  # Worker-only LRU touch, once per process.
    _remember(sidecar, identity, entries)
    return entries


def _remember(sidecar, identity, entries):
    _files[sidecar] = summary_cache.stamp(sidecar), _json(identity), _producer(), entries
    _files.move_to_end(sidecar)
    while len(_files) > MEMORY:
        _files.popitem(last=False)


def load(path, identity, name, args):
    """Return an exact decoded result, or a miss on any auxiliary failure."""
    summary_cache.worker_only()
    try:
        _require(name in NAMES and _current(path, identity))
        with summary_cache._lock:
            encoded = _entries(path, identity)[_key(name, args)]
        value = _unpack(encoded)
        _validate(name, value)
        _require(_current(path, identity))
        return True, value
    except Exception:
        return False, None


def _merge(path, identity, key, value):
    try:
        entries = dict(_entries(path, identity))
    except Exception:
        entries = {}
    entries.pop(key, None)
    entries[key] = value
    while len(entries) > MAX_ENTRIES:
        del entries[next(iter(entries))]
    raw = _json(
        {"magic": "LD24RES1", "identity": identity, "producer": _producer(), "entries": entries}
    )
    _require(len(raw) <= MAX_RAW_BYTES)
    compressed = zlib.compress(raw, level=1)
    _require(len(compressed) <= MAX_FILE_BYTES)
    return compressed, entries


def _write(path, identity, encoded):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".restmp", delete=False) as out:
            temporary = out.name
            out.write(encoded)
        if not _current(path, identity):
            return False
        os.replace(temporary, path.with_suffix(".res"))
        temporary = None
        return True
    finally:
        if temporary is not None:
            summary_cache._remove(Path(temporary))


def store(path, root, identity, name, args, value):
    """Merge one supported result under the shared sidecar lock; the sidecars are pruned on
    the first write and every ``PRUNE_EVERY`` after (a prune walks the whole history)."""
    global _writes
    summary_cache.worker_only()
    try:
        _validate(name, value)
        encoded = _pack(value)
        _require(len(_json(encoded)) <= MAX_VALUE_BYTES)
        with summary_cache._lock:
            _require(_current(path, identity))
            compressed, entries = _merge(path, identity, _key(name, args), encoded)
            if _write(path, identity, compressed):
                _remember(path.with_suffix(".res"), identity, entries)
            _writes += 1
            if _writes % PRUNE_EVERY == 1:
                summary_cache.prune(root)
    except (OSError, ValueError, TypeError, OverflowError, RecursionError, zlib.error):
        pass  # Disk caches must never prevent learning.
