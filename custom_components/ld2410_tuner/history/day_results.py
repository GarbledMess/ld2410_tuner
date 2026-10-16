"""Results derived from one finished recorded day, kept until that day changes.

Learning and its daily runs read the same finished days again and again. What they derive
from a day (row counts, evidence chunks, quality histograms, guess runs) is kept here, keyed
by the day file's identity (``DayStore.identity``: rewritten by thinning or Trim, it is a new
day) and by the caller's arguments, which include whatever else the result depends on, such
as the labels inside the day (``fingerprint``). So a day is derived again only when it or its
labels change. Live and closing days, and days with guesses still to be written into them,
have no identity and are always derived afresh. Memory only, bounded by entries and size.
"""

from array import array
from bisect import bisect_left, bisect_right
from collections import OrderedDict
from threading import RLock

MAX_ENTRIES = 8192
MAX_BYTES = 64 * 1024 * 1024
_kept = OrderedDict()  # Key -> (value, approximate bytes).
_lock = RLock()
_bytes = 0


def remembered(store, device_id, date, name, args, compute):
    """``compute()`` for the finished ``date``, or what it gave before for the same day and
    arguments. ``args`` must hold everything other than the day the result depends on."""
    identity = store.identity(device_id, date)
    if identity is None:
        return compute()
    key = str(store.root), device_id, date, identity, name, args
    with _lock:
        if key in _kept:
            _kept.move_to_end(key)
            return _kept[key][0]
    value = compute()
    _keep(key, value, _size(value))
    return value


def _keep(key, value, size):
    global _bytes
    with _lock:
        if key in _kept:
            _bytes -= _kept.pop(key)[1]
        _kept[key] = value, size
        _bytes += size
        while len(_kept) > 1 and (len(_kept) > MAX_ENTRIES or _bytes > MAX_BYTES):
            _bytes -= _kept.popitem(last=False)[1][1]


def _size(value):
    """Approximate memory of a kept result (containers, arrays, bytes and scalars)."""
    if isinstance(value, array):
        return 64 + value.itemsize * len(value)
    if isinstance(value, (bytes, bytearray, str)):
        return 48 + len(value)
    if isinstance(value, dict):
        return 64 + sum(_size(k) + _size(v) for k, v in value.items())
    if isinstance(value, (tuple, list)):
        return 56 + sum(8 + _size(item) for item in value)
    return 32


def fingerprint(reader, low, high):
    """What a label reader says inside ``[low, high)``: its edges there and the label of each
    piece between them. Equal fingerprints mean the reader is the same inside the range."""
    edges = reader.edges
    inside = tuple(edges[bisect_right(edges, low) : bisect_left(edges, high)])
    return inside, tuple(reader(t) for t in (low, *inside))


def kept_bytes():
    with _lock:
        return _bytes


def clear():
    global _bytes
    with _lock:
        _kept.clear()
        _bytes = 0
