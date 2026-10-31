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

from .constants import EXACT_BUDGET, MIN_EXACT
from .digest import _pieces
from .exact import Evaluator, bounded, regions
from .physical_index import PhysicalBuilder
from .types import RowBlock, ScorePlan


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

    def __init__(self, days, rid, channels):
        self.days, self.rid = days, rid
        self.channels = tuple(sorted(channels))
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
            return lo, lo + digest.n * digest.resolution, cell_rows(digest, self.channels)
        finally:
            digest.partial.close()

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


def cell_rows(digest, channels):
    """Covered cells and partial pieces, each carrying its cell's maxima."""
    times, ends, index = array("d"), array("d"), []
    size = digest.resolution
    for cell, state in enumerate(digest.cov):
        if state == 1:
            edge = (digest.k0 + cell) * size
            pieces = ((edge, edge + size),)
        elif state == 2:
            pieces = tuple(_pieces(digest.partial, cell))
        else:
            continue
        for a, b in pieces:
            times.append(a)
            ends.append(b)
            index.append(cell)
    columns = [b""] * 18
    for c in channels:
        columns[c] = bytes(map(digest.channels[c].__getitem__, index))
    lo = digest.k0 * size
    builder = PhysicalBuilder(channels, lo, lo + digest.n * size)
    builder.add(RowBlock(times, ends, tuple(columns)))
    return builder.finish()


class CellEvaluator(Evaluator):
    """The common evaluator with timed six-second cells as its rows."""

    mode = "cells"

    def __init__(self, problem, regions, *, directory=None):
        super().__init__(problem, regions, directory=directory)

    def providers(self):
        return {
            rid: CellRows(self.problem.days, rid, radar.channels)
            for rid, radar in self.problem.radars.items()
        }


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
    problem, patterns, plan, *, quiet=False, rank=None, evaluators=None, zones=None
):
    """Scores of the patterns compared at ``plan`` fidelity, start first.

    A ``cells_then_exact`` plan returns only the exactly scored subset; each
    score names its ``pattern_index``. ``rank`` orders cell pre-ranks (default
    base cost); ``evaluators`` lets a caller re-score the same records later.
    """
    patterns = list(patterns)
    owned = evaluators is None
    evaluators = Evaluators(problem, plan) if owned else evaluators
    try:
        indexes = list(range(len(patterns)))
        if plan.mode == "cells_then_exact":
            cells = evaluators.get("cells").scores(patterns, quiet=quiet, zones=zones)
            order = sorted(indexes[1:], key=lambda i: ((rank or _base_cost)(cells[i]), i))
            indexes = indexes[:1] + order[: plan.finalist_limit - 1]
        mode = "cells" if plan.mode == "cells" else "exact"
        chosen = [patterns[i] for i in indexes]
        scores = evaluators.get(mode).scores(chosen, quiet=quiet, zones=zones)
        return [{**score, "pattern_index": i} for i, score in zip(indexes, scores, strict=True)]
    finally:
        if owned:
            evaluators.close()
