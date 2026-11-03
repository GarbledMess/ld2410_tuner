"""One fidelity for every compared pattern, chosen from a deterministic row ledger.

``exact`` scores all compared patterns on physical rows when rows x patterns x
passes fit ``EXACT_BUDGET``. Otherwise a fit pre-ranks every pattern on timed
six-second cells and scores the start plus the best ``budget // (rows x passes)``
exactly (``cells_then_exact``); if even two exceed it, or a fixed bundle must keep
every pattern, all compared patterns use timed cells (``cells``). Quiet-presence
passes share the ledger, so a quiet replay never becomes unbudgeted exact work. An
exact score is never compared with a cell score, and no cell pre-rank is promoted
to a final exact cost.
"""

from array import array
from bisect import bisect_left, bisect_right
from dataclasses import replace
from itertools import compress, repeat
from operator import add, mul

from .constants import EXACT_BUDGET, MIN_EXACT
from .digest import _pieces, cell_ceil, cell_floor
from .exact import Evaluator, bounded, regions
from .search_data import vector_runs
from .types import ScorePlan


def score_regions(problem):
    return bounded(problem, regions(span for zone in problem.zones for span in zone.truth))


def plan_scores(problem, pattern_count, *, quiet=False, comparison=False):
    """Choose the common fidelity before any candidate comparison."""
    windows = score_regions(problem)
    if any(rid not in problem.rows for rid in problem.radars):
        return ScorePlan("cells", windows, 0, pattern_count)
    rows = sum(
        problem.rows[rid].rows_in(a, b) for rid in sorted(problem.radars) for a, b in windows
    )
    work = rows * (2 if quiet else 1)
    if work * pattern_count <= EXACT_BUDGET:
        return ScorePlan("exact", windows, rows, pattern_count)
    limit = EXACT_BUDGET // work
    if comparison or limit < MIN_EXACT:
        return ScorePlan("cells", windows, rows, pattern_count)
    return ScorePlan("cells_then_exact", windows, rows, limit)


class CellRows:
    """Timed six-second cells exposed as rows, one cached day, forward only."""

    def __init__(self, days, rid, channels, *, regions=None):
        self.days, self.rid = days, rid
        self.channels = tuple(sorted(channels))
        self.regions = regions
        self._dates = iter(days.dates(rid))
        self._next = None
        self._loaded = []

    def _peek(self):
        if self._next is None:
            date = next(self._dates, None)
            if date is not None:
                bounds = getattr(self.days, "bounds", None)
                self._next = date, bounds(date) if bounds is not None else None
        return self._next

    def _load(self, date):
        digest = self.days.digest(self.rid, date, self.channels)
        if digest is None:
            return None
        try:
            lo = digest.k0 * digest.resolution
            geometry = digest if self.regions is None else _bounded_digest(digest, self.regions)
            return lo, lo + digest.n * digest.resolution, cell_rows(geometry, self.channels)
        finally:
            close = getattr(digest.partial, "close", None)
            if close is not None:
                close()

    def _advance(self, lo, hi):
        """Load days until one reaches ``hi``; days known to end by ``lo`` stay unread."""
        while (pending := self._peek()) is not None:
            date, bounds = pending
            if _after(bounds, hi):
                return
            self._next = None
            day = None if _before(bounds, lo) else self._load(date)
            if day is not None and self._keep(day, lo, hi):
                return

    def _keep(self, day, lo, hi):
        if day[1] > lo:
            self._loaded.append(day)
        return day[1] >= hi

    def window(self, lo, hi):
        self._loaded = [day for day in self._loaded if day[1] > lo]
        self._advance(lo, hi)
        times, ends = array("d"), array("d")
        columns = {c: bytearray() for c in self.channels}
        for _, _, rows in self._loaded:
            t, e, values = rows.window(lo, hi)
            times.extend(t)
            ends.extend(e)
            for c, column in values.items():
                columns[c] += column
        return times, ends, {c: bytes(v) for c, v in columns.items()}


def _after(bounds, hi):
    return bounds is not None and bounds[0] >= hi


def _before(bounds, lo):
    return bounds is not None and bounds[1] <= lo


class _OffsetPieces:
    """Read original partial geometry lazily after cropping a day's cell grid."""

    def __init__(self, partial, offset):
        self.partial, self.offset = partial, offset

    def __getitem__(self, cell):
        return _pieces(self.partial, cell + self.offset)


def _bounded_digest(digest, regions):
    size = digest.resolution
    lo, hi = digest.k0 * size, (digest.k0 + digest.n) * size
    overlap = [(max(a, lo), min(b, hi)) for a, b in regions if a < hi and b > lo]
    first = max(0, cell_floor(min((a for a, _ in overlap), default=hi), size) - digest.k0)
    stop = min(digest.n, cell_ceil(max((b for _, b in overlap), default=hi), size) - digest.k0)
    return replace(
        digest,
        k0=digest.k0 + first,
        n=stop - first,
        cov=digest.cov[first:stop],
        channels=tuple(column[first:stop] for column in digest.channels),
        partial=_OffsetPieces(digest.partial, first),
    )


class _CellWindow:
    """At most one day's cell geometry, ready for replay without wire encoding."""

    def __init__(self, channels):
        self.times, self.ends = array("d"), array("d")
        self.columns = {c: bytearray() for c in channels}
        self.previous = None

    def append(self, lo, hi, vector):
        if self.ends and self.ends[-1] == lo and vector == self.previous:
            self.ends[-1] = hi
            return
        self.times.append(lo)
        self.ends.append(hi)
        for c, value in zip(self.columns, vector, strict=True):
            self.columns[c].append(value)
        self.previous = vector

    def window(self, lo, hi):
        if hi <= lo:
            return array("d"), array("d"), dict.fromkeys(self.columns, b"")
        a, b = bisect_right(self.ends, lo), bisect_left(self.times, hi)
        times, ends = self.times[a:b], self.ends[a:b]
        if times:
            times[0], ends[-1] = max(lo, times[0]), min(hi, ends[-1])
        return times, ends, {c: bytes(v[a:b]) for c, v in self.columns.items()}


def cell_rows(digest, channels):
    """Coalesce equal selected cell vectors, keeping every partial-coverage gap."""
    columns = tuple(digest.channels[c] for c in channels)
    result = _CellWindow(channels)
    if 2 not in digest.cov:
        return _full_cell_rows(result, digest, columns)
    for start, stop in vector_runs((digest.cov, *columns), digest.n):
        vector = tuple(col[start] for col in columns)
        if 255 not in vector:
            _cell_run(result, digest, start, stop, vector)
    return result


def _full_cell_rows(result, digest, columns):
    """Gather full-cell vector runs by columns rather than scalar row appends."""
    runs = vector_runs((digest.cov, *columns), digest.n)
    invalid = int.from_bytes(digest.cov.translate(bytes(v != 1 for v in range(256))), "little")
    for column in columns:
        invalid |= int.from_bytes(column.translate(bytes(v == 255 for v in range(256))), "little")
    usable = invalid.to_bytes(digest.n, "little").translate(bytes(v == 0 for v in range(256)))
    if len(runs) == digest.n and usable.count(1) == digest.n:
        return _distinct_full_rows(result, digest, columns)
    starts = [a for a, _ in runs]
    stops = [b for _, b in runs]
    keep = bytes(map(usable.__getitem__, starts))
    starts, stops = list(compress(starts, keep)), list(compress(stops, keep))
    result.times = array(
        "d", map(mul, map(add, repeat(digest.k0), starts), repeat(digest.resolution))
    )
    result.ends = array(
        "d", map(mul, map(add, repeat(digest.k0), stops), repeat(digest.resolution))
    )
    result.columns = _gather_cell_columns(result.columns, columns, starts, usable)
    return result


def _gather_cell_columns(channels, columns, starts, usable):
    if len(starts) == usable.count(1):
        flags = usable
    elif len(starts) > len(usable) // 2:
        flags = bytearray(len(usable))
        for at in starts:
            flags[at] = 1
    else:
        return {
            c: bytes(map(col.__getitem__, starts)) for c, col in zip(channels, columns, strict=True)
        }
    return {c: bytes(compress(col, flags)) for c, col in zip(channels, columns, strict=True)}


def _distinct_full_rows(result, digest, columns):
    """A complete grid of distinct vectors needs neither gathering nor coalescing."""
    size, first = digest.resolution, digest.k0 * digest.resolution
    result.times = array("d", list(range(first, first + digest.n * size, size)))
    result.ends = array("d", list(range(first + size, first + (digest.n + 1) * size, size)))
    result.columns = dict(zip(result.columns, columns, strict=True))
    return result


def _cell_run(result, digest, start, stop, vector):
    size = digest.resolution
    if digest.cov[start] == 1:
        result.append((digest.k0 + start) * size, (digest.k0 + stop) * size, vector)
    elif digest.cov[start] == 2:
        for cell in range(start, stop):
            for lo, hi in _pieces(digest.partial, cell):
                result.append(lo, hi, vector)


class CellEvaluator(Evaluator):
    """The common evaluator with timed six-second cells as its rows."""

    mode = "cells"

    def __init__(self, problem, regions, *, directory=None):
        super().__init__(problem, regions, directory=directory)

    def source(self, rid):
        return CellRows(
            self.problem.days, rid, self.problem.radars[rid].channels, regions=self.regions
        )


class Evaluators:
    """Lazily created exact and cell evaluators sharing one plan's regions."""

    def __init__(self, problem, plan, *, directory=None):
        self.problem, self.plan, self.directory = problem, plan, directory
        self._made = {}

    def get(self, mode):
        if mode not in self._made:
            kind = CellEvaluator if mode == "cells" else Evaluator
            self._made[mode] = kind(self.problem, self.plan.regions, directory=self.directory)
        return self._made[mode]

    @property
    def operations(self):
        return sum(e.operations for e in self._made.values())

    @property
    def spilled_bytes(self):
        return sum(e.spilled_bytes for e in self._made.values())

    def close(self):
        for evaluator in self._made.values():
            evaluator.close()
        self._made.clear()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _base_cost(score):
    return score["cost"] if score["cost"] is not None else score["partial_cost"]


def evaluate_patterns(
    problem,
    patterns,
    plan,
    *,
    quiet=False,
    rank=None,
    evaluators=None,
    zones=None,
    keep=1,
    attribution=True,
):
    """Scores of the patterns compared at ``plan`` fidelity, start first.

    A ``cells_then_exact`` plan returns only the exactly scored subset (always
    including the first ``keep`` patterns); each score names its ``pattern_index``. ``rank`` orders cell pre-ranks (default
    base cost); ``evaluators`` lets a caller re-score the same records later.
    """
    patterns = list(patterns)
    owned = evaluators is None
    evaluators = Evaluators(problem, plan) if owned else evaluators
    try:
        indexes = list(range(len(patterns)))
        if plan.mode == "cells_then_exact":
            cells = evaluators.get("cells").scores(
                patterns, quiet=quiet, zones=zones, attribution=attribution
            )
            order = sorted(indexes[keep:], key=lambda i: ((rank or _base_cost)(cells[i]), i))
            indexes = indexes[:keep] + order[: max(0, plan.finalist_limit - keep)]
        mode = "cells" if plan.mode == "cells" else "exact"
        chosen = [patterns[i] for i in indexes]
        scores = evaluators.get(mode).scores(
            chosen, quiet=quiet, zones=zones, attribution=attribution
        )
        return [{**score, "pattern_index": i} for i, score in zip(indexes, scores, strict=True)]
    finally:
        if owned:
            evaluators.close()
