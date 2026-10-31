"""Incremental physical timing, with finalized prefixes even inside a held run.

A pending hold keeps its original start for on-delay. Draining moves only the
output cursor, so a continuation remains the same activation for its consumer.
Inputs are ordered bounded blocks, independent of truth and calendar boundaries.
"""

from collections.abc import Iterable
from itertools import chain

from ...calibration.event_replay import _timing


def _append(items, a, b):
    if b <= a:
        return
    if items and a <= items[-1][1]:
        items[-1] = (items[-1][0], max(b, items[-1][1]))
    else:
        items.append((a, b))


def _take(items, lo, hi):
    out, rest = [], []
    for a, b in items:
        _append(out, max(a, lo), min(b, hi))
        _append(rest, max(a, hi), b)
    return out, rest


class TimingStream:
    """Apply hold, then on/off filters, resetting only at physical coverage gaps."""

    def __init__(self, timing: dict):
        self.hold, self.on, self.off = _timing(timing)
        self.coverage, self.active = [], []
        self.low = self.high = None
        self.pending = None
        self.begin = self.expires = None
        self._processed = self._drained = float("-inf")

    def _held(self, x, y):
        if self.begin is not None and x <= self.expires:
            self.expires = y + self.off
            return
        if self.begin is not None:
            _append(self.active, max(self.begin, self._drained), self.expires)
        self.begin = x + self.on if y - x >= self.on else None
        self.expires = y + self.off

    def _hit(self, x, y):
        if self.pending is not None and x <= self.pending[1]:
            self.pending = (self.pending[0], max(self.pending[1], y + self.hold))
            return
        if self.pending is not None:
            self._held(*self.pending)
        self.pending = (x, y + self.hold)

    def _segment(self, a, b):
        if self.high != a:
            self.close()
            self.low = a
        self.high = b
        self._processed = max(self._processed, b)

    def feed(
        self, segments: Iterable[tuple[float, float]], hits: Iterable[tuple[float, float]]
    ) -> None:
        """Consume one chronological block; hits may straddle coverage edges."""
        hits = iter(hits)
        hit = next(hits, None)
        for a, b in segments:
            self._segment(a, b)
            while hit is not None and hit[1] <= a:
                hit = next(hits, None)
            while hit is not None and hit[0] < b:
                self._hit(max(a, hit[0]), min(b, hit[1]))
                if hit[1] > b:
                    break
                hit = next(hits, None)

    def close(self) -> None:
        """End coverage without advancing beyond the last processed endpoint."""
        if self.low is None:
            return
        _append(self.coverage, self.low, self.high)
        if self.pending is not None:
            self._held(self.pending[0], min(self.high, self.pending[1]))
        if self.begin is not None:
            _append(self.active, max(self.begin, self._drained), min(self.high, self.expires))
        self.low = self.high = self.pending = None
        self.begin = self.expires = None

    def finish(self) -> None:
        self.close()

    def advance(self, until: float) -> None:
        """Declare every input before ``until`` fed, so draining may reach it.

        A segment ending earlier cannot continue: the next row starts at or
        after ``until`` and therefore after a physical gap.
        """
        if until < self._processed:
            raise ValueError("Timing input cannot move backwards")
        if self.high is not None and self.high < until:
            self.close()
        self._processed = until

    def frontier(self) -> float:
        """All timing before the last processed endpoint is now decidable."""
        return self._processed

    def _open_active(self):
        opened = []
        begin, expires = self.begin, self.expires
        if begin is not None:
            _append(opened, begin, min(self.high, expires))
        if self.pending is None:
            return opened
        x, y = self.pending
        y = min(y, self.high)
        if begin is not None and x <= expires:
            _append(opened, begin, min(self.high, y + self.off))
        elif y - x >= self.on:
            _append(opened, x + self.on, min(self.high, y + self.off))
        return opened

    def drain(self, until: float) -> tuple[list, list]:
        """Return finalized pieces; adjacent pieces denote a carried activation."""
        if until > self.frontier() or until < self._drained:
            raise ValueError("Drain must advance within the processed frontier")
        cov, self.coverage = _take(self.coverage, self._drained, until)
        if self.low is not None:
            _append(cov, self.low, min(self.high, until))
            self.low = max(self.low, until)
        opened = self._open_active() if self.high is not None else []
        act, _ = _take(chain(self.active, opened), self._drained, until)
        _, self.active = _take(self.active, self._drained, until)
        self._drained = until
        return cov, act
