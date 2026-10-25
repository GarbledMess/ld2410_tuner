"""Duration evidence with label lineage independent of recording segmentation."""

from bisect import bisect_left, bisect_right
from hashlib import sha256
from math import ceil

from ..const import HISTORY_KEYS
from ..history import dayfile, guesses, spans
from .constants import MIN_AUTO_CONFIDENCE, SAMPLE_SECONDS
from .episodes import Episode

LABELS = ("present", "not_present")


def _cells(physical, reader, owner, start, end):
    if not physical:
        return []
    first, last = max(start, physical[0].start), min(end, physical[-1].end)
    result = []
    for cell in physical:
        low, high = max(start, cell.start), min(end, cell.end)
        cuts = [
            low,
            *reader.edges[bisect_right(reader.edges, low) : bisect_left(reader.edges, high)],
            high,
        ]
        for a, b in zip(cuts, cuts[1:], strict=False):
            if b > a:
                state = reader(a)
                result.append(
                    (a, b, cell.values, state, _parent(reader, a, state, owner, first, last))
                )
    return result


def _parent(reader, timestamp, state, owner, first, last):
    if state not in LABELS:
        return None
    index = bisect_right(reader.edges, timestamp)
    low = max(first, reader.edges[index - 1]) if index else first
    high = min(last, reader.edges[index]) if index < len(reader.edges) else last
    return Episode(low, high, state, owner)


def _human(cells, keys):
    """Each human episode's presence masks and readings, and the empty-labelled readings. A
    run of cells under one parent episode keeps that episode's entries at hand (``_Run``):
    looking each up again by episode, for every cell and gate, was most of a room learn's
    evidence work."""
    keys = tuple(keys)  # A mapping of gates (the learner's thresholds) is read in its order.
    episodes, masks, densities = {}, {}, {}
    empty = {key: [0.0] * 101 for key in keys}
    positions = tuple(HISTORY_KEYS.index(key) for key in keys)
    sinks = [empty[key] for key in keys]
    run = _Run(None, keys)
    for low, high, values, _state, parent in cells:
        if parent is None or not any(values[position] is not None for position in positions):
            continue
        if parent is not run.parent:
            run.merge(masks)
            run, episodes[parent] = _Run(parent, keys), None
        run.add(values, positions, high - low, masks, densities, sinks)
    run.merge(masks)
    presence = {
        key: tuple(mask for (ep, gate), mask in masks.items() if gate == key) for key in keys
    }
    empty = {
        key: [seconds / SAMPLE_SECONDS for seconds in histogram] for key, histogram in empty.items()
    }
    return tuple(episodes), presence, empty, densities


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
    from .quality_evidence import AutomaticEpisode, Overlap, _intersections

    parents = dict(zip(runs, _auto_parents(runs, owner), strict=True))
    automatic, overlaps = {}, {}
    usable = [
        (a, b, parent if state in LABELS else state)
        for a, b, values, state, parent in cells
        if any(values[HISTORY_KEYS.index(key)] is not None for key in keys)
    ]
    for low, high, human, run in _intersections(usable, runs):
        confidence = guesses.confidence(run[3])
        if not MIN_AUTO_CONFIDENCE <= confidence <= 1:
            continue
        duration = high - low
        if human is None:
            _add(automatic, (parents[run], run[4]), confidence * duration, duration)
        elif isinstance(human, Episode):
            _add(overlaps, (human, run[4], run[2]), duration * (human.state == run[2]), duration)
    return (
        tuple(
            AutomaticEpisode(ep, basis, mass / duration, owner)
            for (ep, basis), (mass, duration) in automatic.items()
        ),
        tuple(Overlap(*key, mass, duration) for key, (mass, duration) in overlaps.items()),
    )


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
    physical = list(spans.events(view, device_id, start=start, end=end))
    cells = _cells(physical, reader, owner, start, end)
    clipped = [
        (max(start, r[0]), min(end, r[1]), *r[2:])
        for r in guesses.of(view, device_id, start).runs
        if r[0] < end and r[1] > start and r[1] > r[0]
    ]
    runs = _outside_unknown(clipped, reader)
    human, presence, empty, densities = _human(cells, keys)
    automatic, overlaps = _source_evidence(cells, runs, owner, keys)
    digest = sha256(
        repr((cells, runs, keys, owner, device.get("presence_sources"))).encode()
    ).hexdigest()
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


def _density(densities, keys, state):
    return {
        key: tuple(
            hist for (ep, gate), hist in densities.items() if gate == key and ep.state == state
        )
        for key in keys
    }
