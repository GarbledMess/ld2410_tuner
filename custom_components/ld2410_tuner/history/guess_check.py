"""Which of a radar's recorded guesses its human labels prove carry no information.

A radar records each guess against the references it had at the time, so guesses made before
its labels existed can say one state whatever happens (a radar with no reference said
present through every labelled empty hour, 2026-11-15). Each guess source, ``(state, level,
basis)``, is checked against the radar's human truth (``human_truth``): labelled time is cut
into trials of at most an hour, and a trial's outcome is the share of the time guessed at
that level and basis that guessed the state. The trials of the guessed state give the hit
rate and those of the other state the false rate, each with one pseudo-trial on the guess's
side (a right guess, no false one): without evidence a guess is kept. At even odds the guess
is right ``hit / (hit + false)`` of the time, and under the learner's floor
(``MIN_AUTO_CONFIDENCE``) its source is discarded: a guess wrong through all of about five
labelled hours, or right no more often than it is wrong. Discarded runs stay in place as
abstentions (``guesses.DISCARDED``): no evidence, and no veto of the other radars of a room.
"""

from __future__ import annotations

import time
from bisect import bisect_right
from collections import defaultdict

from ..calibration.constants import MIN_AUTO_CONFIDENCE
from .label_lookup import _label_events, _label_index, label_index

STATES = ("present", "not_present")
OTHER = {"present": "not_present", "not_present": "present"}
TRIAL = 3600.0  # Seconds: longer than any guess's memory, so trials are near independent.


def discarded(runs, truth):
    """The ``(state, level, basis)`` sources of ``runs`` that ``truth`` (ordered disjoint
    ``(start, end, state)`` human spans) proves no better than chance."""
    found = _trials(runs, truth)
    result = set()
    for key in {(run[2], run[3], run[4]) for run in runs}:
        own, other = found[key][key[0]], found[key][OTHER[key[0]]]
        hit = (1 + own[1]) / (1 + own[0])
        false = other[1] / (1 + other[0])
        if hit / (hit + false) < MIN_AUTO_CONFIDENCE:
            result.add(key)
    return result


def _trials(runs, truth):
    """Per source, per human state: ``[trials, successes]``, each trial weighted by the share
    of an hour the source guessed in it."""
    found = defaultdict(lambda: {state: [0.0, 0.0] for state in STATES})
    ends = [run[1] for run in runs]
    for low, high, human in _pieces(truth):
        guessed, said = _guessed(runs, ends, low, high)
        for source, seconds in guessed.items():
            weight = min(1.0, seconds / TRIAL)
            for state in STATES:
                entry = found[(state, *source)][human]
                entry[0] += weight
                entry[1] += weight * said[(state, *source)] / seconds
    return found


def _guessed(runs, ends, low, high):
    """Seconds of ``[low, high)`` guessed per ``(level, basis)``, and per ``(state, level,
    basis)``."""
    guessed, said = defaultdict(float), defaultdict(float)
    index = bisect_right(ends, low)
    while index < len(runs) and runs[index][0] < high:
        run = runs[index]
        overlap = min(high, run[1]) - max(low, run[0])
        if overlap > 0:
            guessed[run[3:5]] += overlap
            said[(run[2], *run[3:5])] += overlap
        index += 1
    return guessed, said


def _pieces(truth):
    for start, end, state in truth:
        while start < end:
            yield start, min(end, start + TRIAL), state
            start += TRIAL


def human_truth(runtime, device_id):
    """Ordered disjoint ``(start, end, state)`` spans of what humans labelled the radar: its
    own labels, else the labels of every room or zone it alone records in, else Empty where
    any room or zone covering it (one holding it, or the room around such a zone) is labelled
    Empty: an empty room is empty for each of its radars, but a shared room's presence is not
    presence each of them must see."""
    saved = _snapshot(runtime, device_id)
    device = _labels_of(runtime, saved, device_id)
    # A live label has no end yet: it reaches as far as the recording does.
    until = getattr(runtime, "_history_until", None) or time.time()
    own = label_index(device, groups=saved is None)
    readers = [own, *_group_indexes(saved, device_id)] if saved else [own]
    edges = sorted({min(until, edge) for reader in readers for edge in reader[0]})
    spans = []
    for low, high in zip(edges, edges[1:], strict=False):
        state = _decided([_at(reader, low) for reader in readers])
        if state is None:
            continue
        if spans and spans[-1][1] == low and spans[-1][2] == state:
            spans[-1] = (spans[-1][0], high, state)
        else:
            spans.append((low, high, state))
    return spans


def _decided(answers):
    """The radar's own answer, else a lone group's, else Empty from a shared group."""
    own, *groups = answers
    if own is not None:
        return own if own in STATES else None
    lone = [state for state, alone in groups if alone and state in STATES]
    if lone:
        return lone[-1]
    return "not_present" if any(state == "not_present" for state, _ in groups) else None


def _at(reader, timestamp):
    times, states = reader[:2]
    index = bisect_right(times, timestamp) - 1
    state = states[index] if index >= 0 else None
    return (state, reader[2]) if len(reader) > 2 else state


def _covering(groups, device_id):
    """The groups holding the radar, and the room around each such zone."""
    covering = [key for key, item in groups.items() if device_id in item.get("device_ids", ())]
    for key in list(covering):
        parent = _parent(groups, key)
        if parent and parent not in covering:
            covering.append(parent)
    return covering


def _manual(saved, key):
    return [
        item for item in saved.get("room_labels", {}).get(key, ()) if item.get("source") == "manual"
    ]


def _group_indexes(saved, device_id):
    """``(times, states, alone)`` of the manual labels of each group covering the radar."""
    groups = saved.get("groups", {})
    for key in _covering(groups, device_id):
        if manual := _manual(saved, key):
            alone = list(groups[key].get("device_ids", ())) == [device_id]
            yield (*_label_index(_label_events(manual)), alone)


def shared_presence(runtime, device_id, *, read_only=False):
    """``(key, members, present spans)`` of each room or zone covering the radar with other
    radars in it: its labelled presence is the room's, seen where any of its radars sees it
    (``human_truth`` leaves it out: no one radar of a shared room must see it).
    ``read_only``: a live runtime's room inputs are read in place, not copied (the caller
    reads the result on the event loop and keeps none of it)."""
    saved = _snapshot(runtime, device_id, read_only=read_only)
    if not saved:
        return []
    groups = saved.get("groups", {})
    until = getattr(runtime, "_history_until", None) or time.time()
    found = []
    for key in _covering(groups, device_id):
        members = tuple(sorted({*groups[key].get("device_ids", ()), device_id}))
        manual = _manual(saved, key)
        if len(members) < 2 or not manual:
            continue
        times, states = _label_index(_label_events(manual))
        present = [
            (low, min(high, until))
            for low, high, state in zip(times, times[1:], states, strict=False)
            if state == "present" and min(high, until) > low
        ]
        if present:
            found.append((key, members, present))
    return found


def _parent(groups, key):
    item = groups.get(key) or {}
    if item.get("kind") != "zone" or item.get("area_id") is None:
        return None
    parent = f"area:{item['area_id']}"
    return parent if parent in groups else None


def _labels_of(runtime, saved, device_id):
    device = runtime.data["devices"].get(device_id) or {}
    if "history_labels" in device or saved is None:
        return device
    return (saved.get("members") or {}).get(device_id) or {}


def _snapshot(runtime, device_id, *, read_only=False):
    """The room inputs covering the radar: a history view's own copy, or a live one (read in
    place when ``read_only``)."""
    data = runtime.data
    if "room_snapshot" in data:
        saved = data["room_snapshot"]
        groups = (saved or {}).get("groups", {}).values()
        return saved if any(device_id in item.get("device_ids", ()) for item in groups) else None
    if getattr(runtime, "hass", None) is None:
        return None
    from ..rooms import attribution

    if read_only:
        return attribution.read(runtime, device_id)
    return attribution.snapshot(runtime, device_id)


def _labels_signature(runtime, device_id):
    """What ``human_truth`` reads, cheaply: a label edit, a room label edit or another view."""
    data = runtime.data
    device = data["devices"].get(device_id) or {}
    return (
        device.get("label_revision", 0),
        len(device.get("history_labels") or ()),
        len(device.get("group_labels") or ()),
        device.get("training_label_start"),
        device.get("training_expires_at"),
        device.get("training_state"),
        tuple(sorted(data.get("room_label_revision", {}).items())),
        id(data.get("room_snapshot")),
    )


def checked(runtime, device_id, runs, reaching):
    """``runs`` with the runs of discarded sources as abstentions. ``reaching(spans)`` reads
    the radar's recorded runs on the days its labelled ``spans`` reach: the only runs that
    decide, so a read never loads days its labels do not reach."""
    from . import guesses

    if not runs or getattr(runtime, "data", None) is None:
        return runs  # Nothing labelled to check against: a bare recording store.
    memo = runtime.__dict__.setdefault("_guess_checks", {})
    labels = _labels_signature(runtime, device_id)
    entry = memo.get(device_id)
    if entry is None or entry[0] != labels:
        entry = memo[device_id] = [labels, human_truth(runtime, device_id), None, set()]
    if not entry[1]:
        return runs
    decisive = reaching(entry[1])
    recorded = (len(decisive), decisive[-1] if decisive else None)
    if entry[2] != recorded:
        entry[2], entry[3] = recorded, discarded(decisive, entry[1])
    if not entry[3]:
        return runs
    return [
        (*run[:3], 0, guesses.DISCARDED) if (run[2], run[3], run[4]) in entry[3] else run
        for run in runs
    ]
