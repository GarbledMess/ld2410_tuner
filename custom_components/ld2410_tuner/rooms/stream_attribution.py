"""Bounded column attribution, split only at truth and physical coverage boundaries."""

from bisect import bisect_left, bisect_right
from functools import lru_cache
from itertools import pairwise
from math import inf, nextafter

from ..calibration.constants import ATTRIBUTION_SUSTAIN_SECONDS, SAMPLE_SECONDS
from ..const import HISTORY_KEYS
from ..history import pace
from ..presence.inference import MIN_REFERENCE
from .event_attribution import Quiet

READABLE = bytes(int(value != 255) for value in range(256))


def pieces(view, device_id, edges, start, end):
    from ..history.fit_rows import CapturedRowSource

    start = max(
        start,
        getattr(view, "_attribution_since", None) or -inf,
        getattr(view, "_physical_history_start", -inf),
    )
    source = CapturedRowSource(view, device_id, tuple(range(len(HISTORY_KEYS))))
    for block in source.blocks(start, end):
        pace.rest()
        if not len(block.times):
            continue
        low, high = block.times[0], block.ends[-1]
        cuts = [low, *edges[bisect_right(edges, low) : bisect_left(edges, high)], high]
        for a, b in pairwise(cuts):
            first, stop = bisect_right(block.ends, a), bisect_left(block.times, b)
            if first < stop:
                yield _slice(block, first, stop, a, b)


def _slice(block, first, stop, low=-inf, high=inf):
    from ..engine.hybrid.types import RowBlock

    times, ends = block.times[first:stop], block.ends[first:stop]
    times[0], ends[-1] = max(times[0], low), min(ends[-1], high)
    return RowBlock(times, ends, tuple(column[first:stop] for column in block.columns))


def baseline(blocks, quiet):
    bins = [[0.0] * 101 for _ in HISTORY_KEYS]
    for block in blocks:
        if quiet(block.times[0]):
            _add_readings(bins, block)
    return {
        i: Quiet([seconds / SAMPLE_SECONDS for seconds in hist])
        for i, hist in enumerate(bins)
        if sum(hist) >= MIN_REFERENCE * SAMPLE_SECONDS
    }


def _add_readings(bins, block):
    seconds = [b - a for a, b in zip(block.times, block.ends, strict=True)]
    for histogram, column in zip(bins, block.columns, strict=True):
        if column.count(255) == len(column):
            continue
        # Keep each bin's additions in physical order, including fractional cuts.
        for value, duration in zip(column, seconds, strict=True):
            if value != 255:
                histogram[value] += duration


def found(blocks, reads, own, baseline):
    from .evidence import _StreamEpisode

    active, result = {}, {group: [] for group in reads}
    for block in blocks:
        _find_block(block, reads, active, result, own, baseline, _StreamEpisode)
    for group in tuple(active):
        _close(active, result, group)
    return result


def _find_block(block, reads, active, result, own, baseline, episode):
    occupied = None
    for group, read in reads.items():
        label = read(block.times[0])
        if label is None or label[0] != "present":
            _close(active, result, group)
            continue
        if occupied is None:
            occupied = [_slice(block, a, b) for a, b in _occupied(block)]
        _update(active, result, group, occupied, own, baseline, episode)


def _occupied(block):
    columns = block.columns
    valid = (
        bytes([1]) * len(block.times)
        if any(255 not in column for column in columns)
        else bytes(map(any, zip(*(column.translate(READABLE) for column in columns), strict=True)))
    )
    first, end = None, None
    for index, (low, high, readable) in enumerate(zip(block.times, block.ends, valid, strict=True)):
        if first is not None and (not readable or low != end):
            yield first, index
            first = None
        if readable and first is None:
            first = index
        end = high
    if first is not None:
        yield first, len(block.times)


def _close(active, found, group):
    run = active.pop(group, None)
    if run is not None:
        found[group].append(((run.start, nextafter(run.end, -inf)), run.result()))


def _update(active, found, group, blocks, own, baseline, episode):
    for block in blocks:
        low = block.times[0]
        if group in active and active[group].end != low:
            _close(active, found, group)
        if group not in active:
            active[group] = episode(low, baseline)
        _add_episode(active[group], block, own(low))


def _add_episode(run, block, own):
    run.states.add(own)
    for gate, quiet in run.baseline.items():
        if gate in run.seen:
            continue
        seen, begin = _sustain(block, gate, quiet.ceiling(), run.begin.get(gate))
        if seen:
            run.seen.add(gate)
        run.begin[gate] = begin
    run.end = block.ends[-1]


def _sustain(block, gate, ceiling, begin):
    above = block.columns[gate].translate(_above(ceiling))
    first, last = above.find(b"\x01"), None
    while first >= 0:
        stop = above.find(b"\x00", first)
        stop = len(above) if stop < 0 else stop
        last = begin if first == 0 and begin is not None else block.times[first]
        if block.ends[stop - 1] - last >= ATTRIBUTION_SUSTAIN_SECONDS:
            return True, last
        if stop < len(above):
            last = None
        first = above.find(b"\x01", stop)
    return False, last


@lru_cache(maxsize=256)
def _above(ceiling):
    return bytes(int(ceiling < value < 255) for value in range(256))
