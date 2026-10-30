"""Bounded reads of captured immutable worker inputs, outside the resident Day cache."""

from math import inf

from . import daystore


class CapturedRowSource:
    def __init__(self, view, device_id, channels):
        self.view = view
        self.device_id = device_id
        self.channels = channels

    def _windows(self, lo, hi):
        hi = min(hi, getattr(self.view, "_history_until", inf))
        for date in self.view._days.days(self.device_id):
            start, end = daystore.bounds(date)
            if start < hi and end > lo:
                yield date, max(start, lo), min(end, hi)

    def blocks(self, lo, hi):
        for date, start, end in self._windows(lo, hi):
            with self.view._days.open_columns(self.device_id, date) as columns:
                if columns is not None:
                    yield from columns.blocks(start, end, self.channels)

    def rows_in(self, lo, hi):
        total = 0
        for date, start, end in self._windows(lo, hi):
            with self.view._days.open_columns(self.device_id, date) as columns:
                if columns is not None:
                    total += columns.rows_in(start, end)
        return total
