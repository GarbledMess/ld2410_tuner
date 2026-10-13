"""Replay each radar's own timing before combining its detection with other radars."""

from array import array
from bisect import bisect_left, bisect_right
from itertools import chain, compress, islice, pairwise, repeat
from operator import and_, gt, lt

from ..calibration.duration import DurationGroup
from ..calibration.timing import merged
from ..calibration.timing_metrics import hit_mask
from .intervals import Timeline, intersect, union

KINDS = ("lower", "possible", "upper")
# What a ``Projector`` remembers, in doubles of span ends (a megabyte holds 131 072).
LIMIT = 1 << 20
ENTRY = 8  # What one remembered window costs besides its spans, in doubles.


def radar(rows, thresholds, timing, barriers=()):
    ordered = rows.in_time_order()
    bits = hit_mask(ordered, thresholds)
    present = DurationGroup(ordered, timing, barriers=barriers, measured=False)
    absent = DurationGroup(ordered, timing, absent=True, barriers=barriers, measured=False)
    return from_bits(present, absent, bits)


def frames(present):
    """``(offset, window, start, end)`` of each window with scored time, in the order of the
    rows; ``offset`` is where the window's samples begin in the radar's hit mask."""
    for offset, window, _confidence in present.windows:
        start = window.times[0] + max(window.hold + window.off, window.on)
        end = window.times[-1]
        if end > start:
            yield offset, window, start, end


def clipped(starts, ends, low, high, *, apart=False, ordered=False):
    """The spans ``(starts, ends)`` cut to ``[low, high]`` and united: the ones that reach into
    it, with nothing left of a span that only touches it. ``ordered`` says that the spans are
    in order and none overlaps the next, so only the first and the last can reach out of it;
    ``apart``, that none touches the next either."""
    if ordered:
        first, stop = bisect_right(ends, low), bisect_left(starts, high)
        starts, ends = list(starts[first:stop]), list(ends[first:stop])
        if starts:
            starts[0], ends[-1] = max(starts[0], low), min(ends[-1], high)
    else:
        keep = list(map(and_, map(gt, ends, repeat(low)), map(lt, starts, repeat(high))))
        starts = list(map(max, compress(starts, keep), repeat(low)))
        ends = list(map(min, compress(ends, keep), repeat(high)))
    wide = list(map(gt, ends, starts))
    if not all(wide):
        starts, ends = list(compress(starts, wide)), list(compress(ends, wide))
    return (starts, ends) if apart else merged(starts, ends, sort=not ordered, ordered=ordered)


def window_spans(present, absent, kind, window, hits, start, end):
    """One window's ``(starts, ends)`` of ``kind`` for its ``hits``, within its scored time."""
    group, latest = (absent, False) if kind == "upper" else (present, kind == "lower")
    starts, ends = group._spans(window, hits, latest)
    return clipped(starts, ends, start, end, apart=group.timed, ordered=window.in_order)


def from_bits(present, absent, bits, kinds=KINDS):
    """The scored time (``coverage``) and the ``kinds`` of ``(start, end)`` spans for the hit
    mask ``bits``. A window with no hit has no span."""
    scored = list(frames(present))
    result = {"coverage": union([(start, end) for _, _, start, end in scored])}
    for kind in kinds:
        spans = (
            window_spans(present, absent, kind, window, hits, start, end)
            for offset, window, start, end in scored
            if (hits := (bits >> offset) & window.mask)
        )
        result[kind] = union(
            span for starts, ends in spans for span in zip(starts, ends, strict=True)
        )
    return result


class Projector:
    """A radar's spans of some ``kinds`` for any hit mask, as ``(starts, ends)`` arrays.

    A window's spans depend on its own hits alone, and a threshold change leaves most windows'
    hits as they were, so most of a new projection is already known. What is known is bounded
    (``limit``, in doubles); when it is full everything is forgotten and learnt again."""

    def __init__(self, present, absent, kinds=KINDS, limit=LIMIT):
        self.present, self.absent, self.kinds, self.limit = present, absent, kinds, limit
        self.frames = list(frames(present))
        self.coverage = union([(start, end) for _, _, start, end in self.frames])
        # Windows are apart in time, so their spans are in order as they come, unless the
        # radar's rows were not.
        self.ordered = all(a[3] < b[2] for a, b in pairwise(self.frames))
        self.known, self.held = {}, 0

    def project(self, bits):
        parts = [(array("d"), array("d")) for _ in self.kinds]
        for index, (offset, window, _start, _end) in enumerate(self.frames):
            if hits := (bits >> offset) & window.mask:
                for (starts, ends), (found_starts, found_ends) in zip(
                    parts, self._window(index, hits), strict=True
                ):
                    starts += found_starts
                    ends += found_ends
        if not self.ordered:
            parts = [tuple(array("d", part) for part in merged(*both, sort=True)) for both in parts]
        return dict(zip(self.kinds, parts, strict=True))

    def _window(self, index, hits):
        found = self.known.get((index, hits))
        if found is None:
            _offset, window, start, end = self.frames[index]
            found = tuple(
                tuple(
                    array("d", part)
                    for part in window_spans(
                        self.present, self.absent, kind, window, hits, start, end
                    )
                )
                for kind in self.kinds
            )
            size = ENTRY + sum(len(starts) + len(ends) for starts, ends in found)
            if self.held + size > self.limit:
                self.known.clear()
                self.held = 0
            self.known[index, hits] = found
            self.held += size
        return found


def united(parts):
    """The union of the spans of several radars, each ``(starts, ends)`` of spans apart and in
    order. A span starts afresh where the spans before it all ended earlier, which is where the
    sorted ends are below the sorted starts; no walk over the spans is needed."""
    return parts[0] if len(parts) == 1 else joined(parts)


def joined(parts):
    """``united`` of the ``parts``, even of one: spans of a radar that touch are one."""
    starts = sorted(chain.from_iterable(starts for starts, _ in parts))
    ends = sorted(chain.from_iterable(ends for _, ends in parts))
    if not starts:
        return [], []
    cut = list(map(lt, ends, islice(starts, 1, None)))
    return (
        [starts[0], *compress(islice(starts, 1, None), cut)],
        [*compress(ends, cut), ends[-1]],
    )


def shared(coverages):
    """The time every radar covers."""
    result = None
    for coverage in coverages:
        result = coverage if result is None else intersect(result, coverage)
    return result or []


def combined(radars):
    return {
        "coverage": shared(item["coverage"] for item in radars.values()),
        **{
            key: union(span for item in radars.values() for span in item[key])
            for key in ("lower", "possible", "upper")
        },
    }


def lookups(replayed):
    return {key: Timeline(spans) for key, spans in replayed.items()}
