"""Connect independent corrections and confirmed observations to room references."""

import time
from collections import Counter
from copy import deepcopy

from ..calibration.constants import MIN_AUTO_CONFIDENCE
from ..const import HISTORY_KEYS
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
    from ..history.labels import _history_label_reader
    from ..history.policy import retention_seconds
    from ..rooms import attribution

    current = profile(device)
    cutoff = time.time() - retention_seconds(runtime.data)
    current["periods"] = [
        p for p in current["periods"] if p["source"] != "human" or p["end"] < cutoff
    ]
    device_id = _device_id(runtime, device)
    room = attribution.room_targets(runtime, device_id)
    # Human room truth's targets (human answers are skipped by ``ROOM_ORIGIN``), else the
    # more confident of its "attributed" guess and the automatic room truth.
    automatic = attribution.auto_targets(runtime, device_id)
    folds = {source: references.Fold(references.empty()) for source in ROOM_SOURCES}
    folds["human"] = references.Fold(current)
    label_at = _history_label_reader(device)
    for timestamp, values, state, source, confidence in _observations(
        runtime, device, label_at, room, automatic
    ):
        folds[source].add((timestamp, values, state, source, confidence))
    for fold in folds.values():
        fold.close()
    _install_room_evidence(current, cutoff, [folds[source] for source in ROOM_SOURCES])
    device.get("auto", {}).pop("filter", None)
    return cutoff


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


def _install_room_evidence(current, cutoff, folds):
    """Automatic room truth feeds references with its own confidence, never as "human":
    room sensor labels as "entity"; the room estimate and human room Present attributed only
    by the radar's baseline (rule 2, an inference) as "radar"."""
    current["periods"] = [p for p in current["periods"] if not p.get("room") or p["end"] < cutoff]
    current["periods"].extend(
        {**period, "room": True} for fold in folds for period in fold.profile["periods"]
    )
    for key in {(p["source"], p["state"]) for p in current["periods"] if p.get("room")}:
        same = [p for p in current["periods"] if (p["source"], p["state"]) == key]
        for old in same[: max(0, len(same) - references.MAX_PERIODS)]:
            current["periods"].remove(old)


ROOM_ORIGIN = {"entity": {"automatic"}, "radar": {"estimate", "attributed"}}
ROOM_SOURCES = tuple(ROOM_ORIGIN)
SOURCE_OF = {origin: source for source, origins in ROOM_ORIGIN.items() for origin in origins}


def _room_human(target):
    from ..rooms import attribution

    return target[0] if attribution.is_human(target) else None


def _latest(read):
    """``read`` remembering its last answer: asked twice in a row about one time, it reads once."""
    asked, answer = [object()], [None]

    def cached(t):
        if t != asked[0]:
            asked[0], answer[0] = t, read(t)
        return answer[0]

    return cached


def _human_label(own, room, timestamp):
    """The radar's own label, else human room truth at ``timestamp`` (the radar's label wins)."""
    if own is None and room is not None:
        return _room_human(room(timestamp))
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


def _observations(runtime, device, label_at, room, automatic):
    """The observations of every sample, from one pass over the history: its human one (the
    radar's own label, else human room truth, which the room ``room`` targets give) and, when
    the radar has no label of its own, the room's automatic one: the more confident of
    ``room`` and ``automatic``."""
    from ..rooms import attribution

    if room is not None:
        room = _latest(room)  # The human pass and the automatic one ask about each sample.
    auto = attribution.select(room, automatic)
    for timestamp, row in runtime._iter_history_samples(device):
        own = label_at(timestamp)
        guess = None if own or auto is None else auto(timestamp)
        if found := _evidence(_human_label(own, room, timestamp), guess):
            values = {
                key: value for key, value in zip(HISTORY_KEYS, row, strict=False) if value <= 100
            }
            for state, source, confidence in found:
                yield timestamp, values, state, source, confidence


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

    rebuilds.mark_stale(runtime, {device_id})
    runtime._auto_runtime.pop(device_id, None)


def reset_entities(device):
    if "reference_profile" in device:
        profile(device)["periods"] = [
            p for p in profile(device)["periods"] if p["source"] == "human"
        ]
    device.get("auto", {}).pop("filter", None)


async def initialize(runtime):
    """Import timestamped human history once, off the Home Assistant event loop."""
    for device_id, device in runtime.data["devices"].items():
        if device.get("reference_profile", {}).get("version") == references.VERSION:
            continue
        snapshot = deepcopy(device)
        view = runtime._history_view(device_id)
        view.data["devices"][device_id] = snapshot
        await runtime.hass.async_add_executor_job(_import_history, view, snapshot)
        device["reference_profile"] = snapshot["reference_profile"]


def _import_history(runtime, device):
    rebuild_human(runtime, device)
    current = profile(device)
    if current["periods"]:
        return
    # Old histogram-only stores have no observation times. Preserve their useful
    # distributions, explicitly recording import time and reduced certainty.
    legacy = device.get("history_legacy_histograms", {})
    timestamp = device.get("history_legacy_since") or time.time()
    for state in references.STATES:
        histograms = _legacy_bins(legacy, state)
        if histograms:
            current["periods"].append(
                {
                    "source": "human",
                    "state": state,
                    "start": timestamp,
                    "end": timestamp,
                    "confidence": 0.8,
                    "count": max(sum(b.values()) for b in histograms.values()),
                    "data": references.encode(histograms),
                    "imported_legacy": True,
                }
            )


def _legacy_bins(legacy, state):
    result = {}
    for key, series in legacy.items():
        if key not in HISTORY_KEYS:
            continue
        bins = {str(i): count for i, count in enumerate(series.get(state, [])[:101]) if count > 0}
        if bins:
            result[key] = bins
    return result
