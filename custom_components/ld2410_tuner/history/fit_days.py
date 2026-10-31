"""Stream finished-day cached digests and refold every live snapshot."""

from array import array

from ..engine.hybrid.digest import fold_day
from ..engine.hybrid.physical_index import PhysicalRows
from . import cell_cache, daystore
from .fit_rows import CapturedRowSource


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
        persistent = self.view._days.result_identity(device_id, date)
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
            cell_cache.store(path, path.parent.parent, identity, channels, digest)
        return digest

    def physical(self, device_id, date, channels):
        """One day's usable rows; an unreadable cached section is rebuilt once."""
        digest = self.digest(device_id, date, channels)
        if digest is None:
            return PhysicalRows(channels, array("d"), array("d"), dict.fromkeys(channels, b""))
        digest.partial.close()
        try:
            digest.physical.geometry()
        except ValueError:
            persistent = self.view._days.result_identity(device_id, date)
            digest = self._fold(device_id, date, channels, persistent)
            digest.partial.close()
        return digest.physical
