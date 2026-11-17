"""Radar timeouts suggested from the radars' own history (read-only: nothing is written).

A radar holds its presence output for its timeout after the last reading above a threshold:
a longer timeout bridges a still person's short dips, and holds every false detection that
much longer. For each candidate timeout, the radar's recorded detections at its thresholds
are held for it and priced as Learn prices them: each second of missed labelled presence at
``PRESENCE_PRICE`` seconds of false light, each false start at ``FALSE_TRIGGER_SECONDS``.
A radar's own presence is what humans labelled it (``guess_check.human_truth``: its own
labels, lone rooms', and Empty of any room or zone covering it); a room or zone shared with
other radars adds its labelled presence, missed only where none of its radars holds
presence (``guess_check.shared_presence``). Empty time adds the radar's trusted Empty
guesses (``guesses.evidence``) outside those labels. The radar's own timeout is always a
candidate. The suggestion is the shortest candidate that keeps at least ``keep`` of the best
candidate's cost reduction over the shortest one: a timeout that works nearly as well as the
cheapest, held no longer than it needs; a reduction under ``NOISE_SHARE`` of the cost is
noise and keeps the shortest. ``keep`` 1 is the cheapest, the shorter on a tie. Without
labelled presence there is nothing to weigh.

A room's presence priority multiplies ``PRESENCE_PRICE`` as it does in Learn: for the room's
shared presence, that room's; for a radar's own labels, the highest of the rooms it is in.
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
NOISE_SHARE = 0.03  # A saving under this share of the cost is noise, not a reason to hold longer.
ROUNDS = 4  # Tuning a room's radars in turn settles in a pass or two.
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


def _candidate(timeout, detections, present, empty, priority=1.0):
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
        "cost": PRESENCE_PRICE * priority * missed + false + FALSE_TRIGGER_SECONDS * starts,
    }


def _chosen(rows, keep):
    """The shortest of ``rows`` (in timeout order) keeping ``keep`` of the best reduction; the
    shortest itself where the best saves under ``NOISE_SHARE`` of its cost."""
    shortest = rows[0]["cost"]
    best = shortest - min(row["cost"] for row in rows)
    if best < NOISE_SHARE * shortest:
        return rows[0]
    return next(row for row in rows if shortest - row["cost"] >= keep * best - 1e-9)


def suggest(view, device_id, thresholds, current=None, keep=KEEP_PERCENT / 100, priorities=None):
    """``suggest_room`` for one radar with no other radar's history at hand."""
    found = suggest_room(
        {device_id: view},
        [device_id],
        {device_id: thresholds},
        {device_id: current},
        keep,
        priorities,
    )
    return found[device_id]


def _own(view, device_id):
    """``(present, empty)``: what humans labelled the radar itself (``human_truth``)."""
    truth = guess_check.human_truth(view, device_id)
    present = _merged((a, b) for a, b, state in truth if state == "present")
    return present, _empty(view, device_id, truth)


def _seconds(spans_):
    return sum(b - a for a, b in spans_)


def _shared(views, targets, thresholds, current):
    """``{room: (members, present)}`` of the rooms ``targets`` share with other radars, where
    every member's history and thresholds are at hand and each one not being tuned has a
    timeout to be held at."""
    found = {}
    for key in targets:
        for group, members, present in guess_check.shared_presence(views[key], key):
            if all(
                member in views
                and member in thresholds
                and (member in targets or current.get(member) is not None)
                for member in members
            ):
                found[group] = (members, present)
    return found


class _Room:
    """The radars of ``targets``' rooms: their detections, each candidate timeout's held
    output, and the labelled presence of every room shared by more than one of them."""

    def __init__(self, views, targets, thresholds, current, priorities=None):
        priorities = priorities or {}
        self.radar_priority = priorities.get("radars", {})
        self.room_priority = priorities.get("rooms", {})
        self.own = {key: _own(views[key], key) for key in targets}
        self.rooms = _shared(views, targets, thresholds, current)
        radars = sorted({*targets, *(m for members, _ in self.rooms.values() for m in members)})
        self.timeouts = {
            key: sorted({*CANDIDATES, *([] if current.get(key) is None else [float(current[key])])})
            for key in radars
        }
        lead_in = max(timeouts[-1] for timeouts in self.timeouts.values())
        self.detections, covered = {}, {}
        for key in radars:
            present, empty = self.own.get(key, ([], []))
            windows = _merged(
                present
                + empty
                + [span for members, room in self.rooms.values() if key in members for span in room]
            )
            self.detections[key], covered[key] = _detections(
                views[key], key, thresholds[key], windows, lead_in
            )
        # Labelled time a radar did not record says nothing about it; a room's, nothing
        # unless all of its radars recorded.
        self.own = {
            key: (_intersect(present, covered[key]), _intersect(empty, covered[key]))
            for key, (present, empty) in self.own.items()
        }
        for group, (members, present) in list(self.rooms.items()):
            for member in members:
                present = _intersect(_merged(present), covered[member])
            self.rooms[group] = (members, present)
        self._active = {}

    def presence(self, key):
        """The labelled presence ``key`` is scored on: its own, and its shared rooms'."""
        return _seconds(self.own[key][0]) + sum(
            _seconds(room) for members, room in self.rooms.values() if key in members
        )

    def active(self, key, timeout):
        if (key, timeout) not in self._active:
            self._active[key, timeout] = _merged((a, b + timeout) for a, b in self.detections[key])
        return self._active[key, timeout]

    def row(self, key, timeout, held):
        """``key``'s candidate ``timeout`` with the other radars held at ``held``."""
        present, empty = self.own[key]
        priority = self.radar_priority.get(key, 1.0)
        found = _candidate(timeout, self.detections[key], present, empty, priority)
        for group, (members, room) in self.rooms.items():
            if key in members:
                lit = _merged(
                    span
                    for member in members
                    for span in self.active(member, timeout if member == key else held[member])
                )
                missed, dark = _missed(lit, room)
                found["missed_seconds"] += missed
                found["dark_spells"] += dark
                found["cost"] += PRESENCE_PRICE * self.room_priority.get(group, 1.0) * missed
        return found

    def rows(self, key, held):
        return [self.row(key, timeout, held) for timeout in self.timeouts[key]]


def suggest_room(views, targets, thresholds, current, keep=KEEP_PERCENT / 100, priorities=None):
    """``{radar: {"timeout", "cheapest", "keep", "candidates"} or None}`` for each radar of
    ``targets``. Each candidate is scored on the radar's own labelled presence and empty time,
    and on the labelled presence of each room it shares with other radars, lit where any of
    them holds presence (the others held at their timeouts). The radars of ``targets`` are
    tuned one at a time, each to the shortest candidate keeping ``keep`` of its best one's
    improvement, until none changes. ``views``, ``thresholds`` and ``current`` (each radar's
    own timeout, a candidate beside the others) are by radar: the targets' and their room
    mates'. ``priorities`` is ``{"radars": {radar: priority}, "rooms": {room: priority}}``
    (absent: 1). A radar without enough labelled presence gets None and keeps its timeout."""
    room = _Room(views, targets, thresholds, current, priorities)
    tuned = [key for key in targets if room.presence(key) >= MINIMUM_PRESENCE_SECONDS]
    held = {
        key: float(current[key]) if current.get(key) is not None else CANDIDATES[0]
        for key in room.timeouts
    }
    for _ in range(ROUNDS):
        changed = False
        for key in tuned:
            pace.rest()
            chosen = _chosen(room.rows(key, held), keep)["timeout"]
            changed |= chosen != held[key]
            held[key] = chosen
        if not changed:
            break
    found = dict.fromkeys(targets)
    for key in tuned:
        rows = room.rows(key, held)
        cheapest = min(rows, key=lambda row: (row["cost"], row["timeout"]))
        found[key] = {
            "timeout": _chosen(rows, keep)["timeout"],
            "cheapest": cheapest["timeout"],
            "keep": keep,
            "candidates": rows,
        }
    return found
