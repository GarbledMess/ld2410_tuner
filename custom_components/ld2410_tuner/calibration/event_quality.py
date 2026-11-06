"""Duration evidence with label lineage independent of recording segmentation."""

from bisect import bisect_left, bisect_right
from collections import deque
from functools import lru_cache, reduce
from hashlib import sha256
from itertools import repeat
from math import ceil
from operator import add, mul, sub

from ..const import HISTORY_KEYS
from ..history import dayfile, guesses, spans
from .constants import MIN_AUTO_CONFIDENCE, SAMPLE_SECONDS
from .episodes import Episode

LABELS = ("present", "not_present")


def collect_streamed(view, device_id, keys, start, end, human_reader, owner):
    """Prepare hybrid quality without changing the legacy collector's callers."""
    from ..engine.hybrid.quality import collect_quality

    return collect_quality(view, device_id, keys, start, end, human_reader, owner)


def _cells(physical, reader, owner, start, end, first, last):
    labels = _LabelCells(reader, owner, first, last)
    for cell in physical:
        low, high = cell.start, cell.end
        if start < low < high < end and labels.low <= low and high <= labels.high:
            yield low, high, cell.values, labels.state, labels.parent
            continue
        low, high = max(start, low), min(end, high)
        while low < high:
            state, parent, edge = labels.at(low)
            stop = min(high, edge)
            yield low, stop, cell.values, state, parent
            low = stop


class _LabelCells:
    def __init__(self, reader, owner, first, last):
        self.reader, self.owner = reader, owner
        self.first, self.last = first, last
        self.low, self.high = 0, 0
        self.state, self.parent = None, None

    def at(self, timestamp):
        if not self.low <= timestamp < self.high:
            edges = self.reader.edges
            index = bisect_right(edges, timestamp)
            self.low = edges[index - 1] if index else float("-inf")
            self.high = edges[index] if index < len(edges) else float("inf")
            self.state = self.reader(timestamp)
            self.parent = _parent(
                self.reader, timestamp, self.state, self.owner, self.first, self.last
            )
        return self.state, self.parent, self.high


def _parent(reader, timestamp, state, owner, first, last):
    if state not in LABELS:
        return None
    index = bisect_right(reader.edges, timestamp)
    low = max(first, reader.edges[index - 1]) if index else first
    high = min(last, reader.edges[index]) if index < len(reader.edges) else last
    return Episode(low, high, state, owner)


def _human(cells, keys):
    human = _Human(keys)
    for cell in cells:
        human.add(cell)
    return human.result()


class _Human:
    """Accumulate each episode in cell order, retaining only masks and histograms."""

    def __init__(self, keys):
        self.keys = tuple(keys)
        self.episodes, self.masks, self.densities = {}, {}, {}
        self.empty = {key: [0.0] * 101 for key in self.keys}
        self.positions = tuple(HISTORY_KEYS.index(key) for key in self.keys)
        self.sinks = [self.empty[key] for key in self.keys]
        self.run = _Run(None, self.keys)

    def add(self, cell):
        low, high, values, _state, parent = cell
        if parent is None or not any(values[position] is not None for position in self.positions):
            return
        if parent is not self.run.parent:
            self.run.merge(self.masks)
            self.run, self.episodes[parent] = _Run(parent, self.keys), None
        self.run.add(values, self.positions, high - low, self.masks, self.densities, self.sinks)

    def result(self):
        self.run.merge(self.masks)
        presence = {
            key: tuple(mask for (ep, gate), mask in self.masks.items() if gate == key)
            for key in self.keys
        }
        empty = {
            key: [seconds / SAMPLE_SECONDS for seconds in histogram]
            for key, histogram in self.empty.items()
        }
        return tuple(self.episodes), presence, empty, self.densities


def _observed(cells, human, digest, *, integer_values=False):
    values_text = lru_cache(maxsize=128, typed=True)(_cell_values)
    previous_parent = previous_state = object()
    pending = []
    digest.update(b"[")
    for cell in cells:
        human.add(cell)
        low, high, values, state, parent = cell
        if parent is not previous_parent or state != previous_state:
            suffix = repr((state, parent))[1:]
            previous_parent, previous_state = parent, state
        # Equal signed zeros have different text and must not share a cache key.
        text = (
            repr(cell)
            if not integer_values and 0 in values
            else f"({low!r}, {high!r}, {values_text(*values)}, {suffix}"
        )
        pending.append(text)
        if len(pending) == 256:
            digest.update(("\0".join(pending) + "\0").encode())
            pending.clear()
        yield cell
    if pending:
        digest.update(("\0".join(pending) + "\0").encode())
    digest.update(b"]")


def _cell_values(*values):
    # Typed arguments distinguish integer and float reading representations.
    return repr(values)


class _Run:
    """One parent episode's mask bits and density histograms while its cells are read; its
    entries are made at its first reading of each gate, in reading order."""

    __slots__ = ("parent", "present", "keys", "tokens", "bits", "histograms")

    def __init__(self, parent, keys):
        self.parent, self.keys = parent, keys
        self.present = parent is not None and parent.state == "present"
        self.tokens, self.bits = [None] * len(keys), [0] * len(keys)
        self.histograms = [None] * len(keys)

    def add(self, values, positions, duration, masks, densities, sinks):
        for gate, position in enumerate(positions):
            value = values[position]
            if value is None:
                continue
            energy = ceil(value)
            histogram = self.histograms[gate] or self._first(gate, masks, densities)
            if self.present:
                self.bits[gate] |= 1 << energy
            else:
                sinks[gate][energy] += duration
            histogram[energy] += duration

    def _first(self, gate, masks, densities):
        token = self.tokens[gate] = (self.parent, self.keys[gate])
        if self.present:
            masks.setdefault(token, 0)
        histogram = densities.get(token)
        if histogram is None:
            histogram = densities[token] = [0.0] * 101
        self.histograms[gate] = histogram
        return histogram

    def merge(self, masks):
        for token, found in zip(self.tokens, self.bits, strict=True):
            if found:
                masks[token] |= found


def _auto_parents(runs, owner):
    grouped = []
    for run in runs:
        if grouped and grouped[-1][1] == run[0] and grouped[-1][2] == run[2]:
            grouped[-1][1] = run[1]
        else:
            grouped.append([run[0], run[1], run[2]])
    result, index = [], 0
    for run in runs:
        while grouped[index][1] <= run[0]:
            index += 1
        low, high, state = grouped[index]
        result.append(Episode(low, high, state, owner))
    return result


def _source_evidence(cells, runs, owner, keys):
    positions = tuple(HISTORY_KEYS.index(key) for key in keys)
    intervals = _usable_intervals(cells, positions) if keys and runs else cells
    return _interval_evidence(intervals, runs, owner, keys)


def _interval_evidence(intervals, runs, owner, keys):
    """Automatic episodes and overlaps from ``(low, high, (label, times, ends))`` intervals."""
    from .quality_evidence import AutomaticEpisode, Overlap, _intersections

    if not runs or not keys:
        deque(intervals, maxlen=0)
        return (), ()
    parents = dict(zip(runs, _auto_parents(runs, owner), strict=True))
    automatic, overlaps = {}, {}
    confidence_by_run = {run: guesses.confidence(run[3]) for run in runs}
    for low, high, interval, run in _intersections(intervals, runs):
        human, times, ends = interval
        confidence = confidence_by_run[run]
        if not MIN_AUTO_CONFIDENCE <= confidence <= 1:
            continue
        if human is None:
            _add_interval(automatic, (parents[run], run[4]), confidence, low, high, times, ends)
        elif isinstance(human, Episode):
            _add_interval(
                overlaps, (human, run[4], run[2]), human.state == run[2], low, high, times, ends
            )
    return (
        tuple(
            AutomaticEpisode(ep, basis, mass / duration, owner)
            for (ep, basis), (mass, duration) in automatic.items()
        ),
        tuple(Overlap(*key, mass, duration) for key, (mass, duration) in overlaps.items()),
    )


def _usable_intervals(cells, positions):
    """Bound geometry while walking labels rather than intersecting every cell."""
    times, ends = [], []
    previous = None
    for a, b, _values, state, parent in _readable_cells(cells, positions):
        human = parent if state in LABELS else state
        if times and (a != ends[-1] or human is not previous or len(times) == 4096):
            yield times[0], ends[-1], (previous, times, ends)
            times, ends = [], []
        times.append(a)
        ends.append(b)
        previous = human
    if times:
        yield times[0], ends[-1], (previous, times, ends)


def _readable_cells(cells, positions):
    first, rest = positions[0], positions[1:]
    return (
        cell
        for cell in cells
        if cell[2][first] is not None or any(cell[2][position] is not None for position in rest)
    )


def _add_interval(target, key, confidence, low, high, times, ends):
    # Keep the original cell-order additions, including their floating-point rounding.
    mass, duration = target.get(key, (0.0, 0.0))
    first, after = bisect_right(ends, low), bisect_left(times, high)
    starts, stops = times[first:after], ends[first:after]
    starts[0], stops[-1] = max(low, starts[0]), min(high, stops[-1])
    seconds = list(map(sub, stops, starts))
    mass = reduce(add, map(mul, repeat(confidence), seconds), mass)
    duration = reduce(add, seconds, duration)
    target[key] = mass, duration


def _add(target, key, mass, duration):
    before, elapsed = target.get(key, (0.0, 0.0))
    target[key] = before + mass, elapsed + duration


def _outside_unknown(runs, reader):
    """``runs`` less every explicit Unknown label: it is a barrier, so a run it interrupts
    becomes two automatic episodes rather than one spanning it."""
    edges = reader.edges
    unknown = [(a, b) for a, b in zip(edges, edges[1:], strict=False) if reader(a) == "unknown"]
    return tuple(
        (low, high, *run[2:])
        for run in runs
        for low, high in dayfile.gaps(run[0], run[1], unknown)
        if high > low
    )


def collect(view, device_id, keys, start, end, human_reader, owner):
    from .quality_evidence import QualityContext, _independent_reader

    device = view.data["devices"][device_id]
    reader = _independent_reader(device, human_reader)
    end = min(end, getattr(view, "_attribution_until", end))
    owner = (device_id,) if owner is None else tuple(owner)
    first, last = spans.bounds(view, device_id, start, end)
    physical = spans.events(view, device_id, start=start, end=end)
    cells = _cells(physical, reader, owner, start, end, first, last)
    clipped = [
        (max(start, r[0]), min(end, r[1]), *r[2:])
        for r in guesses.of(view, device_id, start).runs
        if r[0] < end and r[1] > start and r[1] > r[0]
    ]
    runs = _outside_unknown(clipped, reader)
    accumulator, digest = _Human(keys), sha256()
    automatic, overlaps = _source_evidence(_observed(cells, accumulator, digest), runs, owner, keys)
    human, presence, empty, densities = accumulator.result()
    digest = _fingerprint((), runs, keys, owner, device.get("presence_sources"), digest=digest)
    return QualityContext(
        human,
        automatic,
        overlaps,
        presence,
        empty,
        digest,
        runs,
        start,
        end,
        _density(densities, keys, "present"),
        _density(densities, keys, "not_present"),
    )


def _fingerprint(cells, runs, keys, owner, sources, *, digest=None):
    """Hash rows individually; neither their text nor its encoding spans the window."""
    collections = (cells, runs) if digest is None else (runs,)
    digest = sha256() if digest is None else digest
    for collection in collections:
        digest.update(b"[")
        for item in collection:
            digest.update(repr(item).encode())
            digest.update(b"\0")
        digest.update(b"]")
    digest.update(repr((keys, owner, sources)).encode())
    return digest.hexdigest()


def _density(densities, keys, state):
    return {
        key: tuple(
            hist for (ep, gate), hist in densities.items() if gate == key and ep.state == state
        )
        for key in keys
    }
