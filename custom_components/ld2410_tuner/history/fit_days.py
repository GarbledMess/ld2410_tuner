"""Stream finished-day cached digests and refold every live snapshot."""

from array import array
from collections import OrderedDict

from ..engine.hybrid.constants import SPILL_BYTES
from ..engine.hybrid.digest import fold_day
from ..engine.hybrid.physical_index import PhysicalRows
from . import cell_cache, daystore
from .fit_rows import CapturedRowSource


def _result_identity(days, device_id, date):
    """Disk cell sidecars are worker-only; copied live inputs stay job-private."""
    found = getattr(days, "cell_identity", None) or getattr(days, "result_identity", None)
    return None if found is None else found(device_id, date)


def _cache_root(days, path):
    found = getattr(days, "cell_cache_root", None)
    return path.parent.parent if found is None else found(path)


class _PhysicalCache:
    """Job-owned compressed snapshots; never retain decoded days or file handles."""

    def __init__(self):
        self.entries, self.size = OrderedDict(), 0

    def get(self, key):
        found = self.entries.get(key)
        if found is None:
            return None
        self.entries.move_to_end(key)
        return PhysicalRows.from_snapshot(found[0])

    def put(self, key, rows):
        size = rows.encoded_size() + 1024 + len(str(key).encode()) + 384 * len(rows._blocks())
        if size > SPILL_BYTES:
            return
        while self.entries and self.size + size > SPILL_BYTES:
            _, (_, removed) = self.entries.popitem(last=False)
            self.size -= removed
        self.entries[key] = rows.snapshot(), size
        self.size += size


def _physical_cache(days):
    shared = getattr(days, "shared", None)
    if shared is None:
        return None
    if not hasattr(shared, "_fit_physical"):
        shared._fit_physical = _PhysicalCache()
    return shared._fit_physical


def _cached_physical(persistent, channels):
    if persistent is None:
        return None
    path, identity = persistent
    physical = cell_cache.load_physical(path, identity, channels)
    if physical is not None:
        try:
            physical.validate()
            return physical
        except ValueError:
            physical.close()
    return None


class FittingDays:
    def __init__(self, view):
        self.view = view

    def dates(self, device_id):
        yield from self.view._days.days(device_id)

    def bounds(self, date):
        return daystore.bounds(date)

    def digest(self, device_id, date, channels):
        if date not in self.view._days.days(device_id):
            return None
        persistent = _result_identity(self.view._days, device_id, date)
        if persistent is not None:
            path, identity = persistent
            cached = cell_cache.load(path, identity, channels)
            if cached is not None:
                return cached
        return self._fold(device_id, date, channels, persistent)

    def _fold(self, device_id, date, channels, persistent):
        start, end = daystore.bounds(date)
        rows = CapturedRowSource(self.view, device_id, channels)
        digest = fold_day(rows.blocks(start, end), start, end, channels)
        if persistent is not None:
            path, identity = persistent
            cell_cache.store(path, _cache_root(self.view._days, path), identity, channels, digest)
        return digest

    def physical(self, device_id, date, channels):
        """One day's usable rows; an unreadable cached section is rebuilt once."""
        if date not in self.view._days.days(device_id):
            return PhysicalRows(channels, array("d"), array("d"), dict.fromkeys(channels, b""))
        persistent = _result_identity(self.view._days, device_id, date)
        cache = _physical_cache(self.view._days)
        key = None
        if persistent is not None:
            path, identity = persistent
            key = (
                path,
                tuple(identity[:2]),
                tuple(sorted(channels)),
                min(daystore.bounds(date)[1], getattr(self.view, "_history_until", float("inf"))),
            )
            physical = None if cache is None else cache.get(key)
            if physical is not None:
                return physical
        physical = _cached_physical(persistent, channels)
        if physical is None:
            digest = self._fold(device_id, date, channels, persistent)
            digest.partial.close()
            physical = digest.physical
            physical.validate()
        if cache is not None and key is not None:
            cache.put(key, physical)
        return physical
