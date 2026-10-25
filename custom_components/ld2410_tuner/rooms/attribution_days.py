"""Room attribution made a day at a time, so finished days are reused between jobs.

``event_attribution`` attributes a radar's Present episodes over a window from its cells.
Here the same answer is built from per-day parts: each day's quiet readings (its share of the
baseline) and, per group, the day's runs of Present cells with what deciding them needs. A
finished day's part is kept with the day's derived results (``day_results``; on disk beside
the day file in a worker), keyed by what every reader answers within the day, so Seen by
and learning recount only the day being recorded and the window's first, partial day.

A run's part keeps, per gate, its lowest reading, its leading and trailing stretches above
each level (as running minima with the times they reach) and the highest ceiling it stays
above for the sustain time inside (``_threshold``): enough to join runs across midnight and
decide the episode they make, with durations taken between the cells' own times as
``event_attribution._sustained`` takes them.
"""

import re
from array import array
from bisect import bisect_left, bisect_right
from itertools import accumulate
from math import ceil, inf
from operator import neg

from ..calibration.constants import (
    ATTRIBUTION_MARGIN,
    ATTRIBUTION_SUSTAIN_SECONDS,
    SAMPLE_SECONDS,
)
from ..const import HISTORY_KEYS
from ..history import day_results, daystore, spans
from ..presence.inference import MIN_REFERENCE

NAME = "attribution"
VERSION = 1
LEVELS = 101  # Ceilings at or above 100 are above every reading.
# The lowest ceiling a baseline sets (``Quiet.ceiling``): stretches at or below it never count.
FLOOR = ATTRIBUTION_MARGIN


def found(runtime, device_id, edges, reads, own):
    """``event_attribution._found``'s ``found`` from per-day parts."""
    from . import event_attribution

    parts = [
        _part(runtime, device_id, segment, edges, reads, own)
        for segment in _segments(runtime, device_id)
    ]
    bins = [[0.0] * 101 for _ in HISTORY_KEYS]
    for part in parts:
        for gate, seconds in part[0].items():
            for index, value in enumerate(seconds):
                bins[gate][index] += value
    baseline = {
        gate: event_attribution.Quiet([seconds / SAMPLE_SECONDS for seconds in histogram])
        for gate, histogram in enumerate(bins)
        if sum(histogram) >= MIN_REFERENCE * SAMPLE_SECONDS
    }
    return {
        group: [_episode(chain, baseline) for chain in _chains(parts, position)]
        for position, group in enumerate(reads)
    }


def _segments(runtime, device_id):
    """``(low, high, date, whole)`` of each recorded day within the window, ``whole`` when the
    window holds the finished day entire."""
    since = getattr(runtime, "_attribution_since", 0) or -inf
    since = max(since, getattr(runtime, "_physical_history_start", -inf))
    until = getattr(runtime, "_attribution_until", inf)
    days = getattr(runtime, "_days", None)
    if days is None or until <= since:
        return []
    live = days.live_date(device_id)
    found = []
    for date in days.days(device_id):
        start, end = daystore.bounds(date)
        low, high = max(since, start), min(until, end)
        if high > low:
            found.append((low, high, date, (low, high) == (start, end) and date != live))
    return found


def _part(runtime, device_id, segment, edges, reads, own):
    low, high, date, whole = segment
    inside = tuple(edges[bisect_right(edges, low) : bisect_left(edges, high)])

    def compute():
        return _measure(runtime, device_id, low, high, edges, reads, own)

    if not whole:
        return compute()
    key = (VERSION, low, high, inside, _answers(own, low, inside)) + tuple(
        (group, _answers(read, low, inside)) for group, read in reads.items()
    )
    return day_results.remembered(runtime._days, device_id, date, NAME, key, compute)


def _answers(read, low, inside):
    """What ``read`` answers at the day's start, at each edge and between edges: readers here
    change only at edges, so equal answers mean equal attribution inside the day."""
    points = (low, *inside)
    between = [(a + b) / 2 for a, b in zip(points, points[1:], strict=False)]
    return tuple(_plain(read(point)) for point in (*points, *between))


def _plain(answer):
    return tuple(answer) if isinstance(answer, (list, tuple)) else answer


def _measure(runtime, device_id, low, high, edges, reads, own):
    """One day's part: its quiet readings per gate, and per group its runs of Present cells."""
    from . import attribution, event_attribution

    physical = spans.events(runtime, device_id, start=low, end=high)
    cells = list(event_attribution._cells(physical, edges))
    places = event_attribution._places(cells, edges)
    quiet = event_attribution._read_cells(attribution.quiet_reader(own, reads), cells, places)
    bins = [[0.0] * 101 for _ in HISTORY_KEYS]
    for (start, end, values), still in zip(cells, quiet, strict=True):
        if still:
            event_attribution._add_readings(bins, values, end - start)
    owned = event_attribution._read_cells(own, cells, places)
    runs = tuple(
        tuple(
            _run(cells[first:stop], owned[first:stop])
            for first, stop in event_attribution._episodes(
                cells, event_attribution._read_cells(read, cells, places)
            )
        )
        for read in reads.values()
    )
    kept = {gate: array("d", histogram) for gate, histogram in enumerate(bins) if any(histogram)}
    return kept, runs


def _run(cells, owned):
    """``(start, end, own Present, own Empty, gates)`` of one run of Present cells, the
    gates' profiles packed into one array (``_packed``) so a busy day's part stays small."""
    starts, ends = [cell[0] for cell in cells], [cell[1] for cell in cells]
    columns = zip(*(cell[2] for cell in cells), strict=True)
    gates = [_gate(_levels(column), starts, ends) for column in columns]
    return cells[0][0], cells[-1][1], "present" in owned, "not_present" in owned, _packed(gates)


def _packed(gates):
    """Each gate as ``lowest, threshold, count, level, time, ..., count, level, time, ...``."""
    values = array("d")
    for lowest, leading, trailing, threshold in gates:
        values.extend((lowest, threshold))
        for minima in (leading, trailing):
            values.append(len(minima))
            for level, time in minima:
                values.extend((level, time))
    return values


def _unpacked(values):
    gates, position = [], 0
    for _ in HISTORY_KEYS:
        lowest, threshold = int(values[position]), int(values[position + 1])
        position += 2
        minima = []
        for _side in range(2):
            count = int(values[position])
            pairs = values[position + 1 : position + 1 + 2 * count]
            minima.append(tuple((int(pairs[i]), pairs[i + 1]) for i in range(0, 2 * count, 2)))
            position += 1 + 2 * count
        gates.append([lowest, minima[0], minima[1], threshold])
    return gates


def _gate(levels, starts, ends):
    """``(lowest, leading, trailing, threshold)`` of one gate's ``levels`` over a run (bytes:
    each reading's ceiling, 0 for none), searched in C a ceiling at a time."""
    leading = _minima(levels, ends, lambda index: index)
    trailing = _minima(levels[::-1], ends, lambda index: len(levels) - 1 - index, starts)
    return min(levels), leading, trailing, _threshold(levels, starts, ends)


def _levels(column):
    """A gate's readings as bytes of ceilings (0 for no reading, which is above no ceiling)."""
    try:
        return bytes(column)  # Whole readings, as recorded.
    except (TypeError, ValueError):
        return bytes(0 if value is None else max(0, ceil(value)) for value in column)


def _minima(levels, ends, position, starts=None):
    """``((level, time), ...)``: the running minimum of ``levels`` above ``FLOOR`` and the
    farthest time it reaches at each (``ends``; ``starts`` for the trailing side, ``levels``
    reversed): the stretch from the run's edge above a ceiling ``c`` reaches the time of the
    last pair whose level is above ``c``. The minimum is run in C and bisected per level."""
    running = list(accumulate(levels, min))
    found, index = [], 0
    while index < len(running) and (level := running[index]) > FLOOR:
        index = bisect_right(running, -level, key=neg)  # Past the last reading at this level.
        last = position(index - 1)
        found.append((level, ends[last] if starts is None else starts[last]))
    return tuple(found)


def _threshold(levels, starts, ends):
    """The highest ceiling some stretch of the run stays above for the sustain time (-1 for
    none at or above ``FLOOR``): stretches shrink as the ceiling rises, so it is bisected."""
    low, high = FLOOR + 1, LEVELS - 1  # Levels: every reading at or above the level.
    best = -1
    while low <= high:
        level = (low + high) // 2
        if _sustained(levels, starts, ends, level):
            best, low = level - 1, level + 1
        else:
            high = level - 1
    return best


def _sustained(levels, starts, ends, level):
    for match in _AT_OR_ABOVE[level].finditer(levels):
        if ends[match.end() - 1] - starts[match.start()] >= ATTRIBUTION_SUSTAIN_SECONDS:
            return True
    return False


# Stretches of readings at or above each level.
_AT_OR_ABOVE = [re.compile(rb"[\x%02x-\xff]+" % level) for level in range(LEVELS)]


def _chains(parts, position):
    """The group's runs across the days, those that meet joined into one episode."""
    chain = None
    for run in (run for _bins, runs in parts for run in runs[position]):
        if chain is not None and chain.end == run[0]:
            chain.join(run)
            continue
        if chain is not None:
            yield chain
        chain = _Chain(run)
    if chain is not None:
        yield chain


class _Chain:
    """An episode made of one or more runs, joined left to right."""

    def __init__(self, run):
        self.start, self.end, self.present, self.empty, gates = run
        self.gates = _unpacked(gates)

    def join(self, run):
        start, end, present, empty, gates = run
        for gate, other in zip(self.gates, _unpacked(gates), strict=True):
            gate[:] = _joined(gate, other)
        self.end, self.present, self.empty = end, self.present or present, self.empty or empty


def _joined(left, right):
    """One gate's profile over two runs that meet, ``left`` then ``right``."""
    lowest = min(left[0], right[0])
    cross = -1
    for level in range(LEVELS - 1, FLOOR - 1, -1):
        begin, finish = _reach(left[2], level), _reach(right[1], level)
        if begin is not None and finish is not None:
            if finish - begin >= ATTRIBUTION_SUSTAIN_SECONDS:
                cross = level
                break
    leading = _extended(left[1], left[0], right[1])
    trailing = _extended(right[2], right[0], left[2])
    return [lowest, leading, trailing, max(left[3], right[3], cross)]


def _reach(minima, level):
    """The time the stretch from the run's edge above ``level`` reaches (None: none)."""
    found = None
    for lowest, time in minima:
        if lowest <= level:
            break
        found = time
    return found


def _extended(near, near_lowest, far):
    """``near``'s minima continued over the run beyond it: the running minimum of ``near``'s
    readings then ``far``'s, which goes on only while every reading of ``near`` reads."""
    pairs = list(near)
    if near_lowest <= FLOOR:
        return tuple(pairs)
    for lowest, time in far:
        level = min(near_lowest, lowest)
        if pairs and pairs[-1][0] == level:
            pairs[-1] = (level, time)
        else:
            pairs.append((level, time))
    return tuple(pairs)


def _episode(chain, baseline):
    from . import event_attribution

    span = chain.start, event_attribution._closed(chain.end)
    return span, _decision(chain, baseline)


def _decision(chain, baseline):
    for owned, answer in ((chain.present, "yes"), (chain.empty, "no")):
        if owned:
            return {"state": answer, "confidence": 1.0, "inferred": False}
    if not baseline:
        return {"state": "unknown", "confidence": 0.0, "inferred": True}
    from ..calibration.constants import ATTRIBUTION_FULL_CONFIDENCE_SAMPLES

    seen = [gate for gate, quiet in baseline.items() if chain.gates[gate][3] >= quiet.ceiling()]
    mass = max(baseline[gate].mass for gate in seen or baseline)
    return {
        "state": "yes" if seen else "no",
        "inferred": True,
        "confidence": min(1.0, mass / ATTRIBUTION_FULL_CONFIDENCE_SAMPLES),
    }
