"""Representative scored cores with separately budgeted physical timing context."""

from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from math import fsum, inf, isfinite
from types import SimpleNamespace

from ..const import HISTORY_KEYS
from ..history import day_results, daystore, spans
from . import evidence_budget as budget


@dataclass(frozen=True)
class Plan:
    cores: tuple
    contexts: tuple
    audit: dict
    _starts: tuple = field(init=False, repr=False, compare=False)
    _ends: tuple = field(init=False, repr=False, compare=False)
    _mass: tuple = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        mass = [0.0]
        for core in self.cores:
            mass.append(mass[-1] + (core.end - core.start) * core.scale)
        object.__setattr__(self, "_starts", tuple(core.start for core in self.cores))
        object.__setattr__(self, "_ends", tuple(core.end for core in self.cores))
        object.__setattr__(self, "_mass", tuple(mass))

    @property
    def status(self):
        return self.audit["status"]

    def scale_at(self, timestamp):
        index = bisect_right(self._starts, timestamp) - 1
        if index >= 0 and timestamp < self.cores[index].end:
            return self.cores[index].scale
        return 0

    def weights(self, times):
        return [self.scale_at(timestamp) for timestamp in times]

    def seconds(self, intervals):
        return fsum(self._integral(high) - self._integral(low) for low, high in _merged(intervals))

    def _integral(self, timestamp):
        index = bisect_right(self._ends, timestamp)
        mass = self._mass[index]
        if index < len(self.cores) and self.cores[index].start < timestamp:
            mass += (timestamp - self.cores[index].start) * self.cores[index].scale
        return mass


@dataclass(frozen=True)
class _DayRef:
    store: object
    device_id: str
    date: object
    until: object

    def load(self):
        return self.store.day(self.device_id, self.date, until=self.until)

    def remembered(self, name, args, compute):
        """What ``compute(day)`` gives for this day (None when nothing is recorded), derived
        again only when a finished day or ``args`` change."""

        def derive():
            day = self.load()
            return None if day is None else compute(day)

        return day_results.remembered(self.store, self.device_id, self.date, name, args, derive)


def _day_refs(view, device_id, start, end):
    windows = spans._read_windows(view, device_id, start, end)
    until = getattr(view, "_history_until", None)
    for date in spans._store_days(view, device_id, start, end):
        selected = _intersect(windows, [daystore.bounds(date)])
        if selected:
            yield _DayRef(view._days, device_id, date, until), selected


def plan(view, device_id, keys, start, end, timing, reader, limit=50_000):
    """None means exact evidence fits; unsupported plans never omit a required halo."""
    if type(limit) is not int or limit < 1:
        raise ValueError("Evidence row limit must be a positive integer")
    halo = _halo(timing)
    start = max(start, getattr(view, "_physical_history_start", -inf))
    until = getattr(view, "_history_until", None)
    end = min(end, getattr(view, "_attribution_until", inf), inf if until is None else until)
    days = list(_day_refs(view, device_id, start, end))
    original = sum(_row_count(day, windows) for day, windows in days)
    if original <= limit:
        return None
    gates = [HISTORY_KEYS.index(key) for key in keys]
    return _bounded(days, gates, reader, limit, original, halo)


def _halo(timing):
    values = [timing.get(key, 0) or 0 for key in ("timeout", "on_delay", "off_delay")]
    if any(not isfinite(value) or value < 0 for value in values):
        raise ValueError("Timing context must be finite and nonnegative")
    return fsum(values)


def _bounded(days, gates, reader, limit, original, halo):
    cap = budget.MAX_CHUNK_ROWS
    last = {"original_rows": original, "limit": limit, "halo_seconds": halo}
    while cap:
        chunks, owners = _chunks(days, gates, reader, cap)
        strata = len({chunk.state for chunk in chunks})
        totals = _totals(chunks)
        last.update(chunk_rows=cap)
        last.update(
            strata=strata,
            allowed_rows=limit + (budget.MAX_CHUNK_ROWS + 1) * strata,
            overrun_per_stratum=budget.MAX_CHUNK_ROWS + 1,
            observed_seconds=totals,
        )
        if not chunks:
            return _unsupported(last, "no_scored_evidence")
        result = _attempts(days, gates, chunks, owners, limit, halo, last)
        if result is not None:
            return result
        cap = cap // 4 if cap > 1 else 0
    return _unsupported(last, "timing_context_exceeds_budget")


def _attempts(days, gates, chunks, owners, limit, halo, audit):
    allocation = limit
    while True:
        windows = budget.select(chunks, allocation)
        requested = _merged((w.start - halo, w.end + halo) for w in windows)
        contexts = [(day, _intersect(selected, requested)) for day, selected in days]
        rows = sum(_row_count(day, selected) for day, selected in contexts)
        audit.update(context_rows=rows, core_allocation=allocation)
        if rows <= audit["allowed_rows"]:
            return _finish(days, gates, windows, owners, contexts, rows, audit)
        if allocation == 0:
            return None
        allocation //= 2


def _finish(days, gates, windows, owners, contexts, rows, audit):
    by_day = {}
    for index, w in enumerate(windows):
        reference = owners[w.start, w.end, w.state]
        by_day.setdefault(id(reference), (reference, []))[1].append(index)
    observed = {}
    for reference, indexes in by_day.values():
        spans = [(windows[i].start, windows[i].end) for i in indexes]
        observed.update(zip(indexes, _physical_ranges(reference, spans, gates), strict=True))
    cores = tuple(
        budget.Window(low, high, w.state, w.scale)
        for index, w in enumerate(windows)
        for low, high in observed[index]
    )
    real = _merged(
        pair
        for day, selected in contexts
        for found in _physical_ranges(day, selected)
        for pair in found
    )
    core_windows = [(w.start, w.end) for w in cores]
    core_rows = sum(_row_count(day, _intersect(selected, core_windows)) for day, selected in days)
    report = {
        **audit,
        "status": "bounded",
        "context_rows": rows,
        "core_rows": core_rows,
        "represented_seconds": fsum((w.end - w.start) * w.scale for w in cores),
        "sampled_seconds": fsum(w.end - w.start for w in cores),
    }
    return Plan(cores, tuple(real), report)


def _unsupported(audit, reason):
    return Plan((), (), {**audit, "status": "unsupported", "reason": reason})


def _chunks(days, gates, reader, cap):
    found, owners = [], {}
    for reference, windows in days:
        labels = day_results.fingerprint(reader, windows[0][0], windows[-1][1])
        chunks = reference.remembered(
            "chunks",
            (tuple(windows), tuple(gates), cap, labels),
            lambda day, windows=windows: tuple(_day_chunks(day, windows, gates, reader, cap)),
        )
        for chunk in chunks or ():
            found.append(chunk)
            owners[chunk.start, chunk.end, chunk.state] = reference
    return found, owners


def _day_chunks(day, windows, gates, reader, cap):
    for low, high in windows:
        for first, after in _row_pieces(day, low, high, cap):
            clipped = _clipped(day, low, high, first, after)
            read = _clipped_reader(reader, clipped.start)
            for chunk in budget.chunks_of(clipped, read, gates):
                if _scored(chunk.state):
                    yield chunk._replace(
                        start=max(chunk.start, clipped.start),
                        end=min(chunk.end, clipped.end),
                    )


def _scored(state):
    label = state[0] if isinstance(state, tuple) else state
    return label in ("present", "not_present")


def _row_pieces(day, low, high, cap):
    first, after = bisect_right(day.ends, low), bisect_left(day.times, high)
    return ((index, min(after, index + cap)) for index in range(first, after, cap))


def _clipped(day, low, high, first, after):
    times, ends = list(day.times[first:after]), list(day.ends[first:after])
    times[0], ends[-1] = max(low, times[0]), min(high, ends[-1])
    return SimpleNamespace(
        start=times[0],
        end=ends[-1],
        times=times,
        ends=ends,
        columns=[column[first:after] for column in day.columns],
    )


def _row_count(reference, windows):
    if not windows:
        return 0
    return reference.remembered("rows", tuple(windows), lambda day: _rows(day, windows)) or 0


def _rows(day, windows):
    indexes = _merged(
        (bisect_right(day.ends, low), bisect_left(day.times, high)) for low, high in windows
    )
    return sum(after - first for first, after in indexes)


def _physical_ranges(reference, windows, gates=None):
    """Each window's observed ranges in the day: one kept result per day and windows."""
    windows = tuple(windows)
    found = reference.remembered(
        "ranges",
        (windows, None if gates is None else tuple(gates)),
        lambda day: tuple(tuple(_day_ranges(day, low, high, gates)) for low, high in windows),
    )
    return found or tuple(() for _ in windows)


def _day_ranges(day, low, high, gates):
    found = []
    first, after = bisect_right(day.ends, low), bisect_left(day.times, high)
    for index in range(first, after):
        if gates is None or _known(day, index, gates):
            start, end = max(low, day.times[index]), min(high, day.ends[index])
            if end > start:
                _append(found, start, end)
    return found


def _known(day, index, gates):
    return any(
        isfinite(day.columns[gate][index]) and day.columns[gate][index] <= 100 for gate in gates
    )


def _append(found, low, high):
    if found and low <= found[-1][1]:
        found[-1] = found[-1][0], max(high, found[-1][1])
    else:
        found.append((low, high))


def _merged(intervals):
    found = []
    for low, high in sorted(intervals):
        if high > low:
            _append(found, low, high)
    return found


def _intersect(left, right):
    result, index = [], 0
    for low, high in left:
        while index < len(right) and right[index][1] <= low:
            index += 1
        current = index
        while current < len(right) and right[current][0] < high:
            start, end = max(low, right[current][0]), min(high, right[current][1])
            if end > start:
                result.append((start, end))
            current += 1
    return result


def _clipped_reader(reader, start):
    def read(timestamp):
        return reader(max(start, timestamp))

    read.edges = reader.edges
    return read


def _totals(chunks):
    states = dict.fromkeys(chunk.state for chunk in chunks)
    return tuple(
        {"state": state, "seconds": fsum(c.seconds for c in chunks if c.state == state)}
        for state in states
    )
