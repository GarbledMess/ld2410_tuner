"""Overlay request-time evidence on physical cells without inventing labelled time."""

from dataclasses import replace
from itertools import groupby


def _chosen(active):
    if not active:
        return None
    index = max(
        active, key=lambda i: (active[i].state == "unknown", active[i].source == "human", i)
    )
    span = active[index]
    return span if span.state in ("present", "not_present") else None


def _events(truth):
    events = []
    for i, span in enumerate(truth):
        if span.end <= span.start:
            continue
        span = replace(span, dependencies=tuple(dict.fromkeys(span.dependencies)))
        events.extend(((span.start, i, span), (span.end, i, None)))
    return sorted(events, key=lambda event: (event[0], event[1]))


def _resolved(truth):
    active = {}
    previous = None
    for edge, events in groupby(_events(truth), key=lambda event: event[0]):
        span = _chosen(active)
        if previous is not None and span is not None:
            yield previous, edge, span
        for _, index, entering in events:
            if entering is None:
                active.pop(index, None)
            else:
                active[index] = entering
        previous = edge


def _labels(truth):
    pending = None
    for lo, hi, span in _resolved(truth):
        if pending is not None and pending[1] == lo and pending[2] == span:
            pending = pending[0], hi, span
        else:
            if pending is not None:
                yield pending
            pending = lo, hi, span
    if pending is not None:
        yield pending


def overlay(digest, truth):
    """Yield cell, exact overlap endpoints, original lineage, and search purity.

    Explicit Unknown excludes fallback. Human evidence wins its own overlap;
    otherwise the already-admitted automatic evidence remains available. Timing
    and automatic positive-support filtering are deliberately separate concerns.
    """
    coverage = _Coverage(digest._intervals())
    for lo, hi, span in _labels(truth):
        for a, b in coverage.between(lo, hi):
            yield _overlap(digest, a, b, span, lo, hi)


class _Coverage:
    def __init__(self, intervals):
        self.intervals = iter(intervals)
        self.piece = next(self.intervals, None)

    def between(self, lo, hi):
        while self.piece is not None and self.piece[1] <= lo:
            self.piece = next(self.intervals, None)
        while self.piece is not None and self.piece[0] < hi:
            a, b = max(lo, self.piece[0]), min(hi, self.piece[1])
            if a < b:
                yield a, b
            if self.piece[1] > hi:
                break
            self.piece = next(self.intervals, None)


def _overlap(digest, lo, hi, span, label_lo, label_hi):
    from .digest import cell_floor

    cell = cell_floor(lo, digest.resolution) - digest.k0
    edge = (digest.k0 + cell) * digest.resolution
    pure = digest.cov[cell] == 1 and label_lo <= edge and label_hi >= edge + digest.resolution
    return cell, lo, hi, span, pure


def _intersect(left, right):
    left, right = iter(left), iter(right)
    a, b = next(left, None), next(right, None)
    while a is not None and b is not None:
        lo, hi = max(a[0], b[0]), min(a[1], b[1])
        if lo < hi:
            yield lo, hi
        if a[1] <= b[1]:
            a = next(left, None)
        else:
            b = next(right, None)


def zone_segments(digests, members):
    """Intersect only this zone's members, retaining no whole-window masks."""
    if not members or any(member not in digests for member in members):
        return iter(())
    intervals = digests[members[0]].segments()
    for member in members[1:]:
        intervals = _intersect(intervals, digests[member].segments())
    return intervals
