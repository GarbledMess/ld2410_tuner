"""Bounded reads of captured immutable worker inputs, outside the resident Day cache."""

from array import array
from math import inf

from . import daystore


class CapturedRowSource:
    """Raw day-file blocks for folding; cached usable rows for exact replay.

    ``window``/``rows_in`` read the label-independent physical rows stored with
    the finished day's cell sidecar (folded once for a live day), so replay and
    row budgets agree on cold and warm fits without decoding the day file again.
    Only the most recent day is retained.
    """

    def __init__(self, view, device_id, channels):
        self.view = view
        self.device_id = device_id
        self.channels = channels
        self._day = None

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

    def _physical(self, date):
        if self._day is None or self._day[0] != date:
            from .fit_days import FittingDays

            self._day = date, FittingDays(self.view).physical(self.device_id, date, self.channels)
        return self._day[1]

    def rows_in(self, lo, hi):
        return sum(self._physical(date).rows_in(a, b) for date, a, b in self._windows(lo, hi))

    def window(self, lo, hi):
        """Usable rows overlapping ``[lo, hi)``, clipped, as C arrays and bytes."""
        times, ends = array("d"), array("d")
        columns = {c: bytearray() for c in sorted(self.channels)}
        for date, a, b in self._windows(lo, hi):
            t, e, values = self._physical(date).window(a, b)
            times.extend(t)
            ends.extend(e)
            for c, column in values.items():
                columns[c] += column
        return times, ends, {c: bytes(v) for c, v in columns.items()}
