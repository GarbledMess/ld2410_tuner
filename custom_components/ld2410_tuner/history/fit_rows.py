"""Bounded reads of captured immutable worker inputs, outside the resident Day cache."""

from array import array
from bisect import bisect_left, bisect_right
from contextlib import nullcontext
from math import ceil, inf, isfinite

from . import column_cache, daystore, pace


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

    def _columns(self, date):
        days = self.view._days
        if hasattr(days, "open_columns"):
            return days.open_columns(self.device_id, date)
        # The integration's own store (service fallback, tests): its resident day.
        until = getattr(self.view, "_history_until", None)
        return nullcontext(ResidentColumns(days.day(self.device_id, date, until=until)))

    def blocks(self, lo, hi):
        for date, start, end in self._windows(lo, hi):
            cached = self._cached_columns(date, start, end)
            if cached is not None:
                yield from cached
                # As closing the day file would: rows from a replaced source are rejected.
                self.view._days.identity(self.device_id, date)
            else:
                with self._columns(date) as columns:
                    if columns is not None:
                        yield from columns.blocks(start, end, self.channels)

    def _cached_columns(self, date, start, end):
        from .fit_days import _cache_root, _result_identity

        days = self.view._days
        persistent = _result_identity(days, self.device_id, date)
        if persistent is None:
            return None
        path, identity = persistent
        found = column_cache.load(path, identity, start, end, self.channels)
        if found is None:
            lo, hi = daystore.bounds(date)
            with self._columns(date) as columns:
                if columns is None:
                    return None
                column_cache.store(
                    path,
                    _cache_root(days, path),
                    identity,
                    columns.blocks(lo, hi, tuple(range(18))),
                )
            found = column_cache.load(path, identity, start, end, self.channels)
        return found

    def _physical(self, date):
        if self._day is None or self._day[0] != date:
            from .fit_days import FittingDays

            self._day = date, FittingDays(self.view).physical(self.device_id, date, self.channels)
        return self._day[1]

    def _rebuilt(self, date):
        """A cached block failed its check when decoded: fold this day again, once."""
        from .fit_days import FittingDays

        rows = FittingDays(self.view).rebuilt_physical(self.device_id, date, self.channels)
        self._day = date, rows
        return rows

    def _day_rows_in(self, date, lo, hi):
        try:
            return self._physical(date).rows_in(lo, hi)
        except ValueError:
            return self._rebuilt(date).rows_in(lo, hi)

    def _day_window(self, date, lo, hi):
        try:
            return self._physical(date).window(lo, hi)
        except ValueError:
            return self._rebuilt(date).window(lo, hi)

    def rows_in(self, lo, hi):
        from .fit_days import FittingDays

        days = FittingDays(self.view)
        return sum(
            days.rows_in(self.device_id, date, self.channels, a, b, self._day_rows_in)
            for date, a, b in self._windows(lo, hi)
        )

    def window(self, lo, hi):
        """Usable rows overlapping ``[lo, hi)``, clipped, as C arrays and bytes."""
        times, ends = array("d"), array("d")
        columns = {c: bytearray() for c in sorted(self.channels)}
        for date, a, b in self._windows(lo, hi):
            t, e, values = self._day_window(date, a, b)
            times.extend(t)
            ends.extend(e)
            for c, column in values.items():
                columns[c] += column
        return times, ends, {c: bytes(v) for c, v in columns.items()}


def _byte_column(column, first, after):
    if isinstance(column, (bytes, bytearray)):
        return bytes(column[first:after])
    return bytes(ceil(v) if isfinite(v) and 0 <= v <= 100 else 255 for v in column[first:after])


class ResidentColumns:
    """``blocks`` over an already resident Day, matching the worker's captured reader."""

    def __init__(self, day):
        self.day = day

    def blocks(self, lo, hi, channels):
        from ..engine.hybrid.constants import ROW_BLOCK_ROWS
        from ..engine.hybrid.types import RowBlock

        day = self.day
        if day is None or hi <= lo:
            return
        first, after = bisect_right(day.ends, lo), bisect_left(day.times, hi)
        missing = len(day.columns)
        for begin in range(first, after, ROW_BLOCK_ROWS):
            stop = min(after, begin + ROW_BLOCK_ROWS)
            times = array("d", (max(lo, t) for t in day.times[begin:stop]))
            ends = array("d", (min(hi, e) for e in day.ends[begin:stop]))
            columns = tuple(
                _byte_column(day.columns[c], begin, stop)
                if c in channels and c < missing
                else bytes([255]) * (stop - begin)
                for c in range(18)
            )
            yield RowBlock(times, ends, columns)
            pace.rest()
