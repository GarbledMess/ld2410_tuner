"""One day's attribution cells as columns: physical rows cut at the readers' edges.

Readers here change their answer only at edges, so a day is read once per stretch of cells in
one place (between two edges, or on the edge itself) and each stretch is applied in C. Python
runs per edge and per episode; only a quiet histogram runs per reading, a gate at a time, to
keep each bin's float additions in physical order.
"""

from array import array
from bisect import bisect_left, bisect_right
from itertools import compress, pairwise
from operator import ne, sub
from re import finditer

from ..history import pace

_READS = bytes(int(value != 255) for value in range(256))


def cut(times, ends, columns, edges):
    """``(times, ends, columns)`` with each row split at the edges strictly inside it."""
    if not times:
        return times, ends, columns
    inside = edges[bisect_right(edges, times[0]) : bisect_left(edges, ends[-1])]
    cuts = []
    for edge in inside:
        row = bisect_right(times, edge) - 1
        if times[row] < edge < ends[row]:
            cuts.append((row, edge))
    if not cuts:
        return times, ends, columns
    return _split(times, ends, columns, cuts)


def _split(times, ends, columns, cuts):
    new_times, new_ends, pieces = array("d"), array("d"), []
    at, begin = 0, times[0]
    for row, edge in cuts:
        _extend(new_times, times[at : row + 1], begin, new_ends, ends[at : row + 1], edge)
        pieces.append((at, row + 1))
        at, begin = row, edge
    _extend(new_times, times[at:], begin, new_ends, ends[at:], ends[-1])
    pieces.append((at, len(times)))
    return (
        new_times,
        new_ends,
        [b"".join(column[a:b] for a, b in pieces) for column in columns],
    )


def _extend(new_times, starts, begin, new_ends, stops, finish):
    starts[0], stops[-1] = begin, finish
    new_times.extend(starts)
    new_ends.extend(stops)


class Cells:
    """A day's cut rows and the stretches its readers are read over."""

    def __init__(self, times, ends, columns, edges, named):
        self.times, self.ends, self.columns, self.named = times, ends, columns, named
        self.count = len(times)
        self.firsts = _places(times, edges)
        self.stretches = list(pairwise((*self.firsts, self.count)))
        self._flags = self._gaps = None

    def read(self, reader):
        """``reader``'s answer for each stretch, at the stretch's first cell."""
        times = self.times
        return [reader(times[first]) for first in self.firsts]

    def between(self, answers, first, stop):
        """The ``answers`` of the stretches that cells ``[first, stop)`` fall in."""
        firsts = self.firsts
        return answers[bisect_right(firsts, first) - 1 : bisect_left(firsts, stop)]

    def quiet_bins(self, answers):
        """Per gate with any, seconds of quiet readings per ceiling (``answers``: quiet)."""
        flags = self._mask(answers, bool)
        if 1 not in flags:
            return {}
        seconds = list(map(sub, compress(self.ends, flags), compress(self.times, flags)))
        kept = {}
        for gate, column in enumerate(self.columns):
            pace.rest()
            histogram = _histogram(bytes(compress(column, flags)), seconds)
            if any(histogram):
                kept[gate] = array("d", histogram)
        return kept

    def episodes(self, labels):
        """``(first, stop)`` of each run of contiguous readable cells that ``labels`` Present."""
        flags = self._mask(labels, _is_present)
        if 1 not in flags:
            return
        readable = self._readable()
        if readable is not None:
            flags = bytes(map(int.__and__, flags, readable))
        gaps = self._contiguity()
        for match in finditer(b"\x01+", flags):
            first, stop = match.span()
            inside = gaps[bisect_right(gaps, first) : bisect_left(gaps, stop)]
            yield from pairwise((first, *inside, stop))

    def _mask(self, answers, keep):
        flags = bytearray(self.count)
        for (first, stop), answer in zip(self.stretches, answers, strict=True):
            if keep(answer):
                flags[first:stop] = b"\x01" * (stop - first)
        return flags

    def _readable(self):
        """Per cell whether any gate read; None when every cell did."""
        if self._flags is None:
            self._flags = _any_reading(self.columns, self.count)
        return self._flags or None

    def _contiguity(self):
        """The cells that do not start where the cell before them ends."""
        if self._gaps is None:
            different = map(ne, self.ends, self.times[1:])
            self._gaps = list(compress(range(1, self.count), different))
        return self._gaps


def _is_present(label):
    return label is not None and label[0] == "present"


def _places(times, edges):
    """The first cell of each run of cells in one place among ``edges``: an edge starts a new
    place, and a cell on the edge itself is a place of its own."""
    found = [0]
    count = len(times)
    for edge in edges[bisect_left(edges, times[0]) : bisect_right(edges, times[-1])]:
        at = bisect_left(times, edge)
        if at != found[-1]:
            found.append(at)
        if times[at] == edge and at + 1 < count:
            found.append(at + 1)
    return found


def _any_reading(columns, count):
    if any(255 not in column for column in columns):
        return b""
    flags = 0
    for column in columns:
        flags |= int.from_bytes(column.translate(_READS), "little")
    return flags.to_bytes(count, "little")


def _histogram(column, seconds):
    """Seconds per reading, added in physical order; a missing reading lands past the bins."""
    bins = [0.0] * 256
    for value, duration in zip(column, seconds, strict=True):
        bins[value] += duration
    return bins[:101]
