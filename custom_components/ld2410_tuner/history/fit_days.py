"""Stream finished-day cached digests and refold every live snapshot."""

from ..engine.hybrid.digest import fold_day
from . import cell_cache, daystore
from .fit_rows import CapturedRowSource


class FittingDays:
    def __init__(self, view):
        self.view = view

    def dates(self, device_id):
        yield from self.view._days.days(device_id)

    def digest(self, device_id, date, channels):
        if date not in self.view._days.days(device_id):
            return None
        persistent = self.view._days.result_identity(device_id, date)
        if persistent is not None:
            path, identity = persistent
            cached = cell_cache.load(path, identity, channels)
            if cached is not None:
                return cached
        start, end = daystore.bounds(date)
        rows = CapturedRowSource(self.view, device_id, channels)
        digest = fold_day(rows.blocks(start, end), start, end, channels)
        if persistent is not None:
            path, identity = persistent
            cell_cache.store(path, path.parent.parent, identity, channels, digest)
        return digest
