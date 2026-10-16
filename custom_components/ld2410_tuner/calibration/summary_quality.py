"""Dense-trace quality evidence from physical indexes, without vector materialization.

Only the over-budget learner uses this collector. Independent episodes and source
votes retain their original lineage; sampling never multiplies their counts.
"""

from array import array
from bisect import bisect_left, bisect_right
from hashlib import sha256
from math import inf, isnan

from ..const import HISTORY_KEYS
from ..history import day_results, guesses, spans, summaries
from . import event_quality
from .constants import MIN_AUTO_CONFIDENCE, SAMPLE_SECONDS
from .episodes import Episode
from .quality_evidence import (
    AutomaticEpisode,
    Overlap,
    QualityContext,
    _independent_reader,
    _intersections,
)


def _day(view, device_id, date):
    return view._days.day(device_id, date, until=getattr(view, "_history_until", None))


def _extent(day, start, end):
    first = bisect_right(day.ends, start)
    after = bisect_left(day.times, end)
    if first >= after:
        return None
    return max(start, day.times[first]), min(end, day.ends[after - 1])


def _dates(view, device_id, start, end):
    found = []
    for date in spans._store_days(view, device_id, start, end):

        def extent(date=date):
            day = _day(view, device_id, date)
            return None if day is None else _extent(day, start, end)

        found_extent = day_results.remembered(
            view._days, device_id, date, "extent", (start, end), extent
        )
        if found_extent is not None:
            found.append((date, *found_extent))
    return found


def _segments(reader, low, high, owner, first, last):
    cuts = [
        low,
        *reader.edges[bisect_right(reader.edges, low) : bisect_left(reader.edges, high)],
        high,
    ]
    for a, b in zip(cuts, cuts[1:], strict=False):
        state = reader(a)
        yield a, b, state, event_quality._parent(reader, a, state, owner, first, last)


def _accumulate(histograms, keys, parent, episodes, masks, empty, densities):
    if parent is None or not any(any(histograms[HISTORY_KEYS.index(key)]) for key in keys):
        return
    episodes[parent] = None
    for key in keys:
        histogram = histograms[HISTORY_KEYS.index(key)]
        if any(histogram):
            _add_histogram(histogram, key, parent, masks, empty, densities)


def _add_histogram(histogram, key, parent, masks, empty, densities):
    token = parent, key
    mass = densities.get(token)
    if mass is None:
        mass = densities[token] = [0.0] * 101
    for energy, duration in enumerate(histogram):
        mass[energy] += duration
        if parent.state == "present" and duration > 0:
            masks[token] = masks.get(token, 0) | (1 << energy)
        elif parent.state == "not_present":
            empty[key][energy] += duration


def _physical_digest(day, low, high):
    """A digest of the day's physical readings inside ``[low, high)``."""
    digest = sha256()
    first, after = bisect_right(day.ends, low), bisect_left(day.times, high)
    times, ends = day.times[first:after], day.ends[first:after]
    times[0], ends[-1] = low, high
    digest.update(times.tobytes())
    digest.update(ends.tobytes())
    for column in day.columns:
        part = column[first:after]
        digest.update(part if isinstance(part, bytes) else part.tobytes())
    return digest.digest()


def _day_evidence(view, device_id, date, low, high, cuts, gates):
    """What quality evidence needs from one day: its physical digest, each piece's
    histograms of ``gates`` and the observed ranges of any of them."""
    day = _day(view, device_id, date)
    summary = summaries.of_day(day)
    histograms = tuple(
        {gate: array("d", found[gate]) for gate in gates}
        for found in (summary.histograms(a, b) for a, b in cuts)
    )
    return (
        _physical_digest(day, low, high),
        histograms,
        tuple(summary.valid_ranges(low, high, gates)),
    )


def _evidence(view, device_id, dates, reader, keys, owner, digest, accumulators):
    if not dates:
        return
    first, last = dates[0][1], dates[-1][2]
    gates = [HISTORY_KEYS.index(key) for key in keys]
    for date, low, high in dates:
        segments = list(_segments(reader, low, high, owner, first, last))
        cuts = tuple((a, b) for a, b, _state, _parent in segments)
        physical, histograms, valid = day_results.remembered(
            view._days,
            device_id,
            date,
            "quality",
            (low, high, cuts, tuple(gates)),
            lambda date=date, low=low, high=high, cuts=cuts: _day_evidence(
                view, device_id, date, low, high, cuts, gates
            ),
        )
        digest.update(physical)
        for (a, b, state, parent), found in zip(segments, histograms, strict=True):
            digest.update(repr((a, b, state, parent)).encode())
            _accumulate(found, keys, parent, *accumulators)
        coverage = ((x, y, None) for x, y in valid)
        for x, y, _unused, segment in _intersections(coverage, segments):
            _a, _b, state, parent = segment
            yield x, y, parent if state in event_quality.LABELS else state


def _sources(cells, runs, owner):
    parents = dict(zip(runs, event_quality._auto_parents(runs, owner), strict=True))
    automatic, overlaps = {}, {}
    for low, high, human, run in _intersections(cells, runs):
        confidence = guesses.confidence(run[3])
        if not MIN_AUTO_CONFIDENCE <= confidence <= 1:
            continue
        duration = high - low
        if human is None:
            event_quality._add(automatic, (parents[run], run[4]), confidence * duration, duration)
        elif isinstance(human, Episode):
            event_quality._add(
                overlaps, (human, run[4], run[2]), duration * (human.state == run[2]), duration
            )
    return (
        tuple(
            AutomaticEpisode(ep, basis, mass / duration, owner)
            for (ep, basis), (mass, duration) in automatic.items()
        ),
        tuple(Overlap(*key, mass, duration) for key, (mass, duration) in overlaps.items()),
    )


def collect(view, device_id, keys, start, end, human_reader, owner):
    device = view.data["devices"][device_id]
    reader = _independent_reader(device, human_reader)
    end = min(end, getattr(view, "_attribution_until", end))
    if isnan(start) or isnan(end) or end < start:
        raise ValueError("Invalid physical history window")
    physical_start = max(start, getattr(view, "_physical_history_start", -inf))
    owner = (device_id,) if owner is None else tuple(owner)
    dates = _dates(view, device_id, physical_start, end) if end > physical_start else []
    clipped = [
        (max(start, r[0]), min(end, r[1]), *r[2:])
        for r in guesses.of(view, device_id, start).runs
        if r[0] < end and r[1] > start and r[1] > r[0]
    ]
    runs = event_quality._outside_unknown(clipped, reader)
    digest = sha256(
        repr(("physical-summary-v1", runs, keys, owner, device.get("presence_sources"))).encode()
    )
    episodes, masks, densities = {}, {}, {}
    empty = {key: [0.0] * 101 for key in keys}
    usable = _evidence(
        view, device_id, dates, reader, keys, owner, digest, (episodes, masks, empty, densities)
    )
    automatic, overlaps = _sources(usable, runs, owner)
    presence = {
        key: tuple(mask for (ep, gate), mask in masks.items() if gate == key) for key in keys
    }
    empty = {key: [duration / SAMPLE_SECONDS for duration in hist] for key, hist in empty.items()}
    return QualityContext(
        tuple(episodes),
        automatic,
        overlaps,
        presence,
        empty,
        digest.hexdigest(),
        runs,
        start,
        end,
        event_quality._density(densities, keys, "present"),
        event_quality._density(densities, keys, "not_present"),
    )
