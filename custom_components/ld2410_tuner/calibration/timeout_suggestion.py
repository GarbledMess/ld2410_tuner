"""A radar timeout suggested from the radar's own history (read-only: nothing is written).

A radar holds its presence output for its timeout after the last reading above a threshold:
a longer timeout bridges a still person's short dips, and holds every false detection that
much longer. For each candidate timeout, the radar's recorded detections at ``thresholds``
are held for it and priced as Learn prices them: each second of missed labelled presence at
``PRESENCE_PRICE`` seconds of false light, each false start at ``FALSE_TRIGGER_SECONDS``.
Presence is what humans labelled the radar (``guess_check.human_truth``: its own labels,
lone rooms', and Empty of any room or zone covering it). Empty time adds the radar's trusted
Empty guesses (``guesses.evidence``) outside those labels. The radar's own timeout is always
a candidate. The suggestion is the shortest candidate that keeps at least ``keep`` of the
best candidate's cost reduction over the shortest one: a timeout that works nearly as well
as the cheapest, held no longer than it needs. ``keep`` 1 is the cheapest, the shorter on a
tie. Without labelled presence there is nothing to weigh.
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from itertools import compress, pairwise
from operator import ne

from ..const import HISTORY_KEYS
from ..engine.hybrid.constants import FALSE_TRIGGER_SECONDS, PRESENCE_PRICE
from ..history import guess_check, guesses, pace, spans

CANDIDATES = (1.0, 2.0, 3.0, 5.0, 8.0, 12.0, 15.0, 20.0, 30.0)
KEEP_PERCENT = 80  # The default share of the best timeout's improvement to keep.
MINIMUM_PRESENCE_SECONDS = 600.0
SLICE_SECONDS = 21600.0  # History is read at most this much at a time: bounded memory.
_RUN = re.compile(rb"+")


def _merged(intervals):
    out = []
    for low, high in sorted(intervals):
        if out and low <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], high))
        elif high > low:
            out.append((low, high))
    return out


def _empty(view, device_id, truth):
    """Human Empty spans, and the radar's trusted Empty guesses outside every human span."""
    human = _merged((a, b) for a, b, _ in truth)
    labelled = [(a, b) for a, b, state in truth if state == "not_present"]
    # Empty guesses only: read without the quiet veto, which re-reads history for present ones.
    guessed = _merged(
        (run[0], run[1])
        for run in guesses.of(view, device_id).runs
        if run[2] == "not_present" and guesses.counted_empty(run)
    )
    return _merged(labelled + _intersect(guessed, _complement(human)))


def _complement(spans_):
    """The gaps of merged ``spans_`` on the whole line."""
    edges = [-float("inf"), *(x for span in spans_ for x in span), float("inf")]
    return [(edges[i], edges[i + 1]) for i in range(0, len(edges), 2) if edges[i] < edges[i + 1]]


def _intersect(one, other):
    out, i, j = [], 0, 0
    while i < len(one) and j < len(other):
        low, high = max(one[i][0], other[j][0]), min(one[i][1], other[j][1])
        if low < high:
            out.append((low, high))
        if one[i][1] < other[j][1]:
            i += 1
        else:
            j += 1
    return out


def _slices(windows):
    for low, high in windows:
        while low < high:
            yield low, min(high, low + SLICE_SECONDS)
            low += SLICE_SECONDS


def _detections(view, device_id, thresholds, windows, lead_in):
    """``(detections, coverage)`` within ``windows``: recorded intervals at or above a
    threshold (read with a ``lead_in`` of the longest candidate, so a detection held into a window
    is seen), and the time the radar recorded at all. Read a slice at a time; consecutive
    readings become one interval, never one object per reading."""
    tables = {
        index: bytes(1 if float(thresholds[key]) < value <= 100 else 0 for value in range(256))
        for index, key in enumerate(HISTORY_KEYS)
        if key in thresholds
    }
    found, covered = [], []
    for low, high in _slices(windows):
        pace.rest()
        times, ends, columns, _ = spans.event_columns(
            view, device_id, start=low - lead_in, end=high
        )
        count = len(times)
        if not count:
            continue
        breaks = [0, *compress(range(1, count), map(ne, times[1:], ends)), count]
        covered.extend((max(low, times[a]), min(high, ends[b - 1])) for a, b in pairwise(breaks))
        if tables:
            found.extend(_runs(times, ends, columns, tables, breaks))
    return _merged(found), _merged(covered)


def _runs(times, ends, columns, tables, breaks):
    """Intervals of consecutive readings above a threshold, cut at recording gaps."""
    mask = 0
    for index, table in tables.items():
        mask |= int.from_bytes(columns[index].translate(table), "little")
    for match in _RUN.finditer(mask.to_bytes(len(times), "little")):
        first, after = match.span()
        cuts = breaks[bisect_right(breaks, first) : bisect_left(breaks, after)]
        for a, b in pairwise((first, *cuts, after)):
            yield times[a], ends[b - 1]


def _overlap(active, spans_):
    starts = [a for a, _ in active]
    total = 0.0
    for low, high in spans_:
        index = max(0, bisect_right(starts, low) - 1)
        while index < len(active) and active[index][0] < high:
            total += max(0.0, min(high, active[index][1]) - max(low, active[index][0]))
            index += 1
    return total


def _missed(active, present):
    """``(missed seconds, dark spells)`` of the merged ``present`` spans."""
    gaps = _intersect(present, _complement(active))
    return sum(b - a for a, b in gaps), len(gaps)


def _starts_in(active, empty):
    lows = [a for a, _ in empty]
    count = 0
    for start, _ in active:
        index = bisect_right(lows, start) - 1
        if index >= 0 and start < empty[index][1]:
            count += 1
    return count


def _candidate(timeout, detections, present, empty):
    active = _merged((a, b + timeout) for a, b in detections)
    missed, dark = _missed(active, present)
    false = _overlap(active, empty)
    starts = _starts_in(active, empty)
    return {
        "timeout": timeout,
        "missed_seconds": missed,
        "dark_spells": dark,
        "false_seconds": false,
        "false_starts": starts,
        "cost": PRESENCE_PRICE * missed + false + FALSE_TRIGGER_SECONDS * starts,
    }


def _chosen(rows, keep):
    """The shortest of ``rows`` (in timeout order) keeping ``keep`` of the best reduction."""
    shortest = rows[0]["cost"]
    best = shortest - min(row["cost"] for row in rows)
    return next(row for row in rows if shortest - row["cost"] >= keep * best - 1e-9)


def suggest(view, device_id, thresholds, current=None, keep=KEEP_PERCENT / 100):
    """``{"timeout", "candidates"}`` (each candidate's missed seconds, dark spells, false
    seconds, false starts and cost), or None without enough labelled presence. ``current``,
    the radar's timeout, is scored beside the candidates."""
    truth = guess_check.human_truth(view, device_id)
    present = [(a, b) for a, b, state in truth if state == "present"]
    if sum(b - a for a, b in present) < MINIMUM_PRESENCE_SECONDS:
        return None
    empty = _empty(view, device_id, truth)
    timeouts = sorted({*CANDIDATES, *([] if current is None else [float(current)])})
    windows = _merged(present + empty)
    detections, covered = _detections(view, device_id, thresholds, windows, timeouts[-1])
    # Labelled time the radar did not record says nothing about it.
    present, empty = _intersect(_merged(present), covered), _intersect(empty, covered)
    if sum(b - a for a, b in present) < MINIMUM_PRESENCE_SECONDS:
        return None
    rows = [_candidate(timeout, detections, present, empty) for timeout in timeouts]
    cheapest = min(rows, key=lambda row: (row["cost"], row["timeout"]))
    return {
        "timeout": _chosen(rows, keep)["timeout"],
        "cheapest": cheapest["timeout"],
        "keep": keep,
        "candidates": rows,
    }
