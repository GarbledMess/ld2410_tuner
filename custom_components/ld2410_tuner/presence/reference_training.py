"""Connect independent corrections and confirmed observations to room references."""

import time
from bisect import bisect_left, bisect_right
from collections import Counter
from itertools import pairwise

from ..calibration.constants import MIN_AUTO_CONFIDENCE
from ..const import HISTORY_KEYS
from ..history import pace
from . import references


def profile(device):
    current = device.get("reference_profile")
    if not current or current.get("version") != references.VERSION:
        current = references.empty()
        device["reference_profile"] = current
    return current


def observe(runtime, device, timestamp, row, label=None, confidence=0.0, *, external=False):
    """Fold one recorded sample into the references: its human label, else the radar's own
    guess (``label`` with ``confidence``, none when ``label`` is None). A guess is evidence
    (``guesses.counted``): the confidence is the calibrated one, and the floor the learner's."""
    manual = _human_state(runtime, device, timestamp)
    if manual == "unknown":
        return  # An explicit exclusion is not permission to use an inferred label.
    values = {key: value for key, value in zip(HISTORY_KEYS, row, strict=False) if value <= 100}
    if manual in references.STATES:
        references.observe(profile(device), values, manual, "human", 1.0, timestamp)
        return
    source = "entity" if external else "radar"
    if label is None or confidence < MIN_AUTO_CONFIDENCE:
        return
    if not external and not profile(device)["periods"]:
        return  # Bootstrap guesses cannot establish an independent reference.
    references.observe(profile(device), values, label, source, confidence, timestamp)


def _human_state(runtime, device, timestamp):
    manual = runtime._manual_history_state(device, timestamp)
    if manual is None and _room_quiet(runtime, device, timestamp):
        return "not_present"  # Every room of this radar is labelled Empty (spec 4.3).
    return manual


def _device_id(runtime, device):
    return next((key for key, value in runtime.data["devices"].items() if value is device), None)


def _room_quiet(runtime, device, timestamp):
    # Live room evidence is Empty only; attributed Present enters through rebuild_human.
    from ..rooms import attribution

    return attribution.room_quiet(runtime, _device_id(runtime, device), timestamp)


def rebuild_human(runtime, device):
    """Rebuild retained human evidence after an edit; keep older learned summaries.
    Returns the retention cutoff: the periods it rewrote are those ``owned`` at it.
    Runs only off the event loop: callers on it go through ``rebuilds.refresh``."""
    from ..history.label_lookup import _history_label_reader
    from ..history.policy import retention_seconds
    from ..rooms import attribution

    current = profile(device)
    cutoff = time.time() - retention_seconds(runtime.data)
    current["periods"] = [
        p for p in current["periods"] if p["source"] != "human" or p["end"] < cutoff
    ]
    device_id = _device_id(runtime, device)
    label_at = _history_label_reader(device)
    room, automatic = None, None
    if not _fully_labelled(runtime, device_id, label_at, cutoff):
        room = attribution.room_targets(runtime, device_id)
        # Automatic room evidence only fills time without the radar's own label.
        automatic = attribution.auto_targets(runtime, device_id)
    profiles = {source: references.empty() for source in ROOM_SOURCES}
    profiles["human"] = current
    _fold_native(runtime, device_id, label_at, room, automatic, profiles, cutoff)
    _install_room_evidence(current, cutoff, [profiles[source] for source in ROOM_SOURCES])
    device.get("auto", {}).pop("filter", None)
    return cutoff


def _reference_end(runtime):
    from math import inf

    until = getattr(runtime, "_history_until", None)
    return min(time.time() if until is None else until, getattr(runtime, "_attribution_until", inf))


def _fully_labelled(runtime, device_id, label_at, cutoff):
    from ..history import summaries

    return summaries.fully_labelled(runtime, device_id, label_at, cutoff, _reference_end(runtime))


def owned(period, cutoff):
    """A period ``rebuild_human`` rewrites: retained human evidence and room evidence."""
    return period["end"] >= cutoff and (period["source"] == "human" or bool(period.get("room")))


def splice(device, periods, cutoff, since):
    """Install a rebuild's ``owned`` ``periods`` into the live profile. Everything else stays
    live: entity and radar observations made, or removed, while the rebuild computed. Owned
    periods started after ``since`` (when the rebuild copied the history) are newer than it
    and are kept after its own; a live period the rebuild extended is replaced by its copy.
    Each source and state keeps its newest ``MAX_PERIODS``, as ``rebuild_human`` does."""
    current = profile(device)
    rebuilt = {_identity(p) for p in periods}
    kept = [p for p in current["periods"] if not owned(p, cutoff) and _identity(p) not in rebuilt]
    newer = [p for p in current["periods"] if owned(p, cutoff) and p["start"] > since]
    current["periods"] = _newest(kept + periods + newer)
    device.get("auto", {}).pop("filter", None)


def _identity(period):
    return period["source"], period["state"], period["start"], bool(period.get("room"))


def _newest(periods):
    """In time order, each source and state's newest ``MAX_PERIODS``: folding finds a
    source's latest period from the end of the list."""
    periods = sorted(periods, key=lambda p: p["start"])
    excess = Counter((p["source"], p["state"]) for p in periods)
    for key in excess:
        excess[key] -= references.MAX_PERIODS
    result = []
    for period in periods:
        key = (period["source"], period["state"])
        if excess[key] > 0:
            excess[key] -= 1
            continue
        result.append(period)
    return result


def _install_room_evidence(current, cutoff, profiles):
    """Automatic room truth feeds references with its own confidence, never as "human":
    room sensor labels as "entity"; the room estimate and human room Present attributed only
    by the radar's baseline (rule 2, an inference) as "radar"."""
    kept = [p for p in current["periods"] if not p.get("room") or p["end"] < cutoff]
    rooms = [{**period, "room": True} for profile in profiles for period in profile["periods"]]
    current["periods"] = _newest(kept + rooms)


ROOM_ORIGIN = {"entity": {"automatic"}, "radar": {"estimate", "attributed"}}
ROOM_SOURCES = tuple(ROOM_ORIGIN)
SOURCE_OF = {origin: source for source, origins in ROOM_ORIGIN.items() for origin in origins}


def _room_human(target):
    from ..rooms import attribution

    return target[0] if attribution.is_human(target) else None


def _human_label(own, target):
    """The radar's own label, else the already-read human room truth."""
    if own is None and target is not None:
        return _room_human(target)
    return own


def _evidence(label, guess):
    """``(state, source, confidence)`` of what a sample teaches: a human ``label``, and the
    room's automatic ``guess`` from the source it came from (``ROOM_ORIGIN``)."""
    found = []
    if label in references.STATES:
        found.append((label, "human", 1.0))
    if guess and guess[1] in SOURCE_OF:
        found.append((guess[0], SOURCE_OF[guess[1]], guess[2]))
    return found


def _span_evidence(label_at, room, automatic, timestamp):
    from ..rooms import attribution

    own = label_at(timestamp)
    target = room(timestamp) if own is None and room is not None else None
    guess = None
    if not own:
        guess = (
            target
            if attribution.is_human(target)
            else attribution.stronger(target, automatic(timestamp) if automatic else None)
        )
    return _evidence(_human_label(own, target), guess)


def correct(runtime, device, ranges):
    """After a device label edit: drop overlapping automatic summaries now (cheap) and mark
    the references stale; the human evidence is rebuilt overnight or on demand."""
    current = profile(device)
    # Entity/inferred periods are summaries, so remove the whole overlapping
    # contribution rather than pretending we can precisely subtract old samples.
    current["periods"] = [
        p
        for p in current["periods"]
        if p["source"] == "human"
        or not any(p["start"] < end and p["end"] >= start for start, end in ranges)
    ]
    device_id = _device_id(runtime, device)
    from . import rebuilds

    # Room peers read this radar's labels for their room truth.
    rebuilds.mark_stale(runtime, {device_id, *_room_peers(runtime, device_id)})
    runtime._auto_runtime.pop(device_id, None)


def _room_peers(runtime, device_id):
    from ..rooms import groups

    _, _, definitions = groups.definitions(runtime)
    return {
        key
        for item in definitions.values()
        if device_id in item["device_ids"]
        for key in item["device_ids"]
    }


def reset_entities(device):
    if "reference_profile" in device:
        profile(device)["periods"] = [
            p for p in profile(device)["periods"] if p["source"] == "human"
        ]
    device.get("auto", {}).pop("filter", None)


async def initialize(runtime):
    """Import timestamped human history once, off the Home Assistant event loop."""
    from ..worker import bridge

    for device_id, device in runtime.data["devices"].items():
        if device.get("reference_profile", {}).get("version") == references.VERSION:
            continue
        result = await bridge.device(
            runtime,
            "references",
            device_id,
            {"jobs": [{"device_id": device_id, "view_key": device_id}]},
        )
        device["reference_profile"] = {**references.empty(), "periods": result[0]["periods"]}


@pace.background
def _import_history(runtime, device):
    rebuild_human(runtime, device)


def _native_values(values):
    from math import ceil

    return {
        key: ceil(value)
        for key, value in zip(HISTORY_KEYS, values, strict=True)
        if value is not None
    }


def _native_edges(readers):
    edges = set()
    for reader in readers:
        edges.update(getattr(reader, "edges", ()))
        edges.update(_native_edges(getattr(reader, "parts", ())))
    return tuple(sorted(edges))


def _native_pieces(cell, edges):
    first, after = bisect_right(edges, cell.start), bisect_left(edges, cell.end)
    yield from pairwise((cell.start, *edges[first:after], cell.end))


def _fold_native(runtime, device_id, label_at, room, automatic, profiles, cutoff):
    """Fold indexed observed ranges without materialising full-vector readings."""
    from math import inf

    from ..history import spans
    from .reference_summary import BatchSummary, RebuildFold

    cutoff = max(cutoff, getattr(runtime, "_physical_history_start", -inf))
    end = _reference_end(runtime)
    if end <= cutoff:
        return
    native = {source: RebuildFold(profile) for source, profile in profiles.items()}
    readers = [read for read in (label_at, room, automatic) if read is not None]
    edges = _native_edges(readers)
    for date in spans._store_days(runtime, device_id, cutoff, end):
        pace.rest()
        day = runtime._days.day(device_id, date, until=getattr(runtime, "_history_until", None))
        if day is not None:
            _fold_batch(BatchSummary(day), cutoff, end, edges, (label_at, room, automatic), native)
    for fold in native.values():
        fold.finish()


def _fold_batch(summary, cutoff, end, edges, readers, native):
    from math import floor

    from ..history.spans import Span

    pieces = []
    for start, stop in summary.coverage_ranges(cutoff, end):
        for low, high in _native_pieces(Span(start, stop, (), "event"), edges):
            found = _span_evidence(*readers, low)
            while found and low < high:
                after = min(
                    high, (floor(low / references.PERIOD_SECONDS) + 1) * references.PERIOD_SECONDS
                )
                pieces.append((low, after, found))
                low = after
    # Bound temporary histogram/snapshot memory even for heavily edited label histories.
    for first in range(0, len(pieces), 128):
        _fold_batch_pieces(summary, pieces[first : first + 128], native)


def _fold_batch_pieces(summary, pieces, native):
    histograms = summary.histograms_many([(low, high) for low, high, _ in pieces])
    for (low, high, found), bins in zip(pieces, histograms, strict=True):
        for state, source, confidence in found:
            native[source].add_histograms(low, high, bins, state, source, confidence)


def observe_span(
    runtime, device, start, end, values, label=None, confidence=0.0, *, external=False
):
    """Causal native reference evidence for one span (see ``observe_spans``)."""
    observe_spans(runtime, device, [(start, end, values, label, confidence, external)])


def observe_spans(runtime, device, items):
    """Causal native reference evidence; automatic confidence is raw, in 0..1.

    ``items`` are ``(start, end, values, label, confidence, external)`` of newly observed
    physical coverage, in time order; one fold takes them all, so a batch decodes and encodes
    the open period once. This adapter applies the same fixed recording/display trust and
    quiet veto as observe(), once, after the raw admission floor. Own inferred guesses cannot
    bootstrap independent references. ``values`` is a complete HISTORY_KEYS vector; missing
    gates are None.
    """
    from ..history.spans import Span

    # Live room evidence is Empty only (``_room_quiet``), which changes only at room label
    # edges; attributed Present enters through ``rebuild_human``, never per poll.
    edges = _live_label_edges(runtime, device)
    fold = None  # Made on the first evidence: no evidence leaves the profile untouched.
    for start, end, values, label, confidence, external in items:
        for low, high in _native_pieces(Span(start, end, tuple(values), "event"), edges):
            found = _native_piece(runtime, device, low, values, label, confidence, external)
            if found is not None:
                fold = fold or references.SpanFold(profile(device))
                fold.add(low, high, _native_values(values), *found)
    if fold is not None:
        fold.close()


def _live_label_edges(runtime, device):
    """Keep label indexes outside persisted devices; live expiry is an indexed edge. Room
    edges are read afresh: sensor labels extend without any revision changing."""
    from ..history.label_lookup import _history_label_reader

    caches = getattr(runtime, "_reference_label_readers", None)
    if caches is None:
        caches = runtime._reference_label_readers = {}
    cache = caches.setdefault(_device_id(runtime, device), {})
    reader = _history_label_reader(device, cache=cache)
    return sorted({*reader.edges, *_room_edges(runtime)})


def _room_edges(runtime):
    for labels in runtime.data.get("room_labels", {}).values():
        for label in labels:
            yield from (label["start"], label["end"])


def _native_piece(runtime, device, low, values, label, confidence, external):
    """``(state, source, confidence)`` a live piece starting at ``low`` adds, or None."""
    manual = _human_state(runtime, device, low)
    if manual == "unknown":
        return None
    if manual in references.STATES:
        return manual, "human", 1.0
    found = _native_confidence(device, values, label, confidence, external)
    return None if found is None else (label, "entity" if external else "radar", found)


def _native_confidence(device, values, label, confidence, external):
    from math import isfinite

    from ..history import guesses

    if label is None or not isfinite(confidence) or not MIN_AUTO_CONFIDENCE <= confidence <= 1:
        return None
    if not external and not profile(device)["periods"]:
        return None
    row = tuple(value if value is not None else 255 for value in values)
    basis = guesses.EXTERNAL if external else guesses.REFERENCE
    return guesses.counted((label, confidence * 100, basis), row, device.get("histograms"))
