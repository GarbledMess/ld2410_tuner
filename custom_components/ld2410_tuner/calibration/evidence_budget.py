"""A bounded, representative evidence budget for learning on dense recordings.

Every recorded change is evidence, and a busy radar records hundreds of thousands a day: the
learner cannot replay a month of them within its time budget. It learns from a budget of
recorded time instead, chosen by value rather than at random:

- kept whole, standing for themselves, while they fit: the newest time of each class, the
  quietest presence (what the gates must still detect), the loudest empty-room noise (what
  they must ignore), then the few minutes beside each label edge (where labels and readings
  disagree most), newest first;
- the rest of each class sampled evenly over its whole history, each sampled stretch standing
  for its share of the class's unsampled time (``Window.scale``), so every class keeps its
  total time and its rates stay unbiased.

The budget counts rows (recorded changes), the learner's cost; chunk summaries (row count,
loudest and quietest reading) come from the day columns without replaying rows.
"""

from bisect import bisect_left, bisect_right
from collections import namedtuple
from math import ceil, fsum, isnan
from operator import sub

Chunk = namedtuple("Chunk", "start end state rows peak floor edge seconds")
Window = namedtuple("Window", "start end state scale")

CHUNK_MINUTES = 10
EDGE_SECONDS = 120
MAX_CHUNK_ROWS = 1_000  # Bounds the one sampled chunk a class may add over the limit.


def select(chunks, limit):
    """The windows to learn from: all of ``chunks`` when their rows fit ``limit``, else the
    valuable ones whole and an even sample of the rest of each class, scaled."""
    if sum(c.rows for c in chunks) <= limit:
        return [Window(c.start, c.end, c.state, 1) for c in chunks]
    keep = _affordable(_valuable(chunks), limit)
    spare = max(0, limit - sum(c.rows for c in keep))
    windows = [Window(c.start, c.end, c.state, 1) for c in keep]
    strata = _strata([c for c in chunks if c not in keep])
    rows = sum(c.rows for rest in strata.values() for c in rest)
    for rest in strata.values():
        share = spare * sum(c.rows for c in rest) / rows if rows else 0
        windows.extend(_sampled(rest, share))
    return sorted(windows)


def _valuable(chunks):
    """The valuable chunks, most valuable first: each stratum's newest chunk and most extreme
    reading, then label edges, newest first. A stratum is a chunk's ``state``: a class, or a
    tuple of the class and its provenance (source, confidence band)."""
    ranked = []
    for state, members in _strata(chunks).items():
        ranked.append(members[-1])
        read = [c for c in members if c.peak is not None]
        state = state[0] if isinstance(state, tuple) else state  # (class, provenance...).
        if read and state == "present":
            ranked.append(min(read, key=lambda c: (c.peak, c.start)))
        elif read and state == "not_present":
            ranked.append(max(read, key=lambda c: (c.peak, -c.start)))
    ranked += sorted((c for c in chunks if c.edge), key=lambda c: -c.start)
    return list(dict.fromkeys(ranked))


def _affordable(ranked, limit):
    """The leading ``ranked`` chunks whose rows fit ``limit`` (skipping any that would not)."""
    keep, rows = set(), 0
    for c in ranked:
        if rows + c.rows <= limit:
            keep.add(c)
            rows += c.rows
    return keep


def _strata(chunks):
    strata = {}
    for c in sorted(chunks, key=lambda c: c.start):
        strata.setdefault(c.state, []).append(c)
    return strata


def _sampled(rest, share):
    """Every ``step``-th chunk of ``rest`` (in time order, centred), at least one, scaled so
    together they stand for all of ``rest``'s time."""
    rows = sum(c.rows for c in rest)
    step = max(1, ceil(rows / share)) if share > 0 else len(rest)
    chosen = rest[step // 2 :: step] or rest[len(rest) // 2 : len(rest) // 2 + 1]
    total = sum(c.seconds for c in rest)
    covered = sum(c.seconds for c in chosen)
    scale = total / covered if covered else 1
    return [Window(c.start, c.end, c.state, scale) for c in chosen]


def chunks_of(day, read, gates, minutes=CHUNK_MINUTES):
    """``day``'s observed time as chunks of at most ``minutes`` and ``MAX_CHUNK_ROWS`` rows,
    cut at every edge of the label reader ``read`` (``read.edges``) and ``EDGE_SECONDS``
    either side of it. A chunk holds every row whose reading covers part of it (a reading
    held from an earlier chunk too), with its label state, row count, loudest and quietest
    reading of ``gates``, whether it is one of the short stretches beside a label edge, and
    its observed seconds: recording gaps and time when every one of ``gates`` was missing
    are not observations. Chunks with no observed time are omitted."""
    if not len(day.times):
        return []
    edges = sorted(t for t in read.edges if day.start < t < day.end)
    grid = range(int(day.start), int(day.end), minutes * 60)
    sides = (t + side for t in edges for side in (-EDGE_SECONDS, EDGE_SECONDS))
    cuts = sorted({*grid, *edges, *(t for t in sides if day.start < t < day.end), day.end})
    found = []
    for low, high in zip(cuts, cuts[1:], strict=False):
        edge = low in edges or high in edges
        for start, end in _pieces(day, low, high):
            found.extend(_chunk(day, read(low), gates, start, end, edge))
    return found


def _rows(day, low, high):
    """The rows whose coverage overlaps ``[low, high)``."""
    return bisect_right(day.ends, low), bisect_left(day.times, high)


def _pieces(day, low, high):
    """``[low, high)`` cut at row times so that no piece starts more than
    ``MAX_CHUNK_ROWS`` rows."""
    first, after = _rows(day, low, high)
    bounds = [low, *(day.times[i] for i in range(first + MAX_CHUNK_ROWS, after, MAX_CHUNK_ROWS))]
    return zip(bounds, [*bounds[1:], high], strict=True)


def _chunk(day, state, gates, low, high, edge):
    first, after = _rows(day, low, high)
    if after <= first:
        return
    peak, floor = _extremes(day, gates, first, after)
    seconds = _seconds(day, gates, first, after, low, high)
    if peak is not None and seconds > 0:
        yield Chunk(low, high, state, after - first, peak, floor, edge, seconds)


def _seconds(day, gates, first, after, low, high):
    """Observed seconds of rows ``first:after`` inside ``[low, high)``."""
    total = fsum(map(sub, day.ends[first:after], day.times[first:after]))  # Not epoch sums.
    total -= max(0, low - day.times[first]) + max(0, day.ends[after - 1] - high)
    for i in _unread(day, gates, first, after):
        total -= min(day.ends[i], high) - max(day.times[i], low)
    return total


def _unread(day, gates, first, after):
    """Rows where every one of ``gates`` is missing (none at all without a missing value
    in every gate, so the usual case costs no per-row work)."""
    parts = [day.columns[gate][first:after] for gate in gates]
    if any(isinstance(part, (bytes, bytearray)) and _all_read(part) for part in parts):
        return []
    return [first + i for i in range(after - first) if all(_missing(part[i]) for part in parts)]


def _all_read(part):
    return len(part.translate(None, UNREAD)) == len(part)


def _missing(value):
    return isnan(value) or value > 100


def _extremes(day, gates, first, after):
    peaks, floors = [], []
    for gate in gates:
        values = _readings(day.columns[gate], first, after)
        if values:
            peaks.append(max(values))
            floors.append(min(values))
    return (max(peaks), min(floors)) if peaks else (None, None)


UNREAD = bytes(range(101, 256))


def _readings(column, first, after):
    part = column[first:after]
    if isinstance(part, (bytes, bytearray)):
        return part.translate(None, UNREAD)  # C-level: no per-row Python work.
    return [value for value in part if not isnan(value)]
