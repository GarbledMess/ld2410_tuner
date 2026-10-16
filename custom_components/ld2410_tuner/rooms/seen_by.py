"""The panel's "Seen by": each group's latest occupied episodes and who saw them (spec 4.3).

Chips come from ``attribution.radar_attribution``, the function the learner's
``room_targets`` and ``auto_targets`` use, so they show what training uses.

Cost: attribution reads every member's full history, and the snapshot is polled every few
seconds. Each group's result is cached against a signature of its inputs (group definitions,
manual and sensor room label revisions of the related groups, member label revisions and
live-training fields, and every member's stored history block set). A poll only compares
signatures; on a mismatch it copies each member's history view on the event loop (list
copies and its room snapshot, as for learning) and rebuilds in the executor, serving the previous result until the new one lands. Builds
run one at a time (executor work holds the GIL).

Freshness: label, revision, membership and training changes rebuild at once. History is
fingerprinted per member (block count, first and last block start and count), and a
history-only change rebuilds at most once per ``HISTORY_REBUILD_SECONDS``. Samples still in
the open block are read by a rebuild but are not in the signature, so new recordings appear
when their block is stored (60 samples) and the interval allows. An open room sensor label
that only extends does not bump ``room_sensor_revision`` (rooms/room_sources.py), so its
growth reaches "Seen by" on the next history rebuild, not at once.
"""

from __future__ import annotations

import asyncio
import logging
import time
from bisect import bisect_left, bisect_right
from copy import deepcopy
from math import inf, nextafter

from ..calibration.constants import MIN_AUTO_CONFIDENCE
from ..history import pace, recording
from ..runtime.tasks import finish_task
from . import attribution, truth

_LOGGER = logging.getLogger(__name__)

EPISODE_LIMIT = 50
# Opt-in approximation for chips only; None keeps all retained evidence. A positive
# number bounds both occupied episodes and their quiet baseline. Older chips disappear,
# ownership/confidence can change, and a cutoff-crossing episode is clipped. Stored human
# labels, references, assessments and learning remain independent of this display policy.
# Seen-by shows the latest episodes: it attributes recent history only (a whole history cost
# minutes of CPU on small machines after every room change).
SEEN_BY_HISTORY_HOURS = 72
# Minimum time between rebuilds caused by new or pruned history alone.
HISTORY_REBUILD_SECONDS = 300
_clock = time.monotonic  # Injectable for tests.
# A grouped member that recorded nothing in an episode: no human label, no baseline.
UNKNOWN = {"state": "unknown", "confidence": 0.0, "inferred": True}


def _scope(definitions, group_id, devices):
    """Groups, related label owners and members whose data can change this group's result."""
    own = set(definitions[group_id]["device_ids"])
    owners = sorted(
        key for key, item in definitions.items() if key == group_id or own & set(item["device_ids"])
    )
    related = truth.related_to(definitions, owners)
    members = sorted(
        {key for owner in owners for key in definitions[owner]["device_ids"] if key in devices}
    )
    return owners, related, members


def signature(runtime, definitions, group_id):
    devices = runtime.data["devices"]
    owners, related, members = _scope(definitions, group_id, devices)
    manual = runtime.data.get("room_label_revision", {})
    sensor = runtime.data.get("room_sensor_revision", {})
    inputs = {
        "history_hours": SEEN_BY_HISTORY_HOURS,
        "groups": deepcopy({key: definitions.get(key) for key in {*owners, *related}}),
        "labels": {key: (manual.get(key, 0), sensor.get(key, 0)) for key in related},
        "members": {key: attribution.member_signature(devices[key]) for key in members},
    }
    history = {key: attribution.history_fingerprint(runtime, key, devices[key]) for key in members}
    return {"inputs": inputs, "history": history}


def _view(runtime, definitions, group_id):
    """Each member's history view (copied on the event loop, as for learning, with its room
    snapshot); None when no member has recorded anything."""
    devices = runtime.data["devices"]
    own = [key for key in definitions[group_id]["device_ids"] if key in devices]
    if not any(recording.has_history(runtime, key) for key in own):
        return None
    # Shares the learner's attribution cache.
    shared = {}  # The members' guesses, built once for the job's views.
    since = None if SEEN_BY_HISTORY_HOURS is None else time.time() - SEEN_BY_HISTORY_HOURS * 3600
    return {
        key: attribution.prepared_view(
            runtime, key, shared, **({} if since is None else {"since": since})
        )
        for key in own
    }


def _as_learned(result):
    """The learner drops a rule-2 (inferred) result below ``MIN_AUTO_CONFIDENCE``: unknown."""
    weak = result.get("inferred") and result["confidence"] < MIN_AUTO_CONFIDENCE
    return {**result, "state": "unknown"} if weak and result["state"] != "unknown" else result


def _pieces(inside, human, truth_at, starts=None):
    """Split one radar's full-truth episode (its sample times ``inside``) where room truth
    is human (``attribution.is_human``): such a sample lies in a human episode and takes its
    result. ``((first, last), human result or None)`` per run of samples with one owner."""
    if starts is None:
        starts = [span[0] for span, _ in human]
    pieces = []
    cuts = _piece_cuts(inside, starts, truth_at)
    for first, after in attribution.stretches(len(inside), cuts):
        t, last = inside[first], inside[after - 1]
        owner = bisect_right(starts, t) - 1 if attribution.is_human(truth_at(t)) else None
        if pieces and pieces[-1][1] == owner:
            pieces[-1][0][1] = last
        else:
            pieces.append(([t, last], owner))
    return [
        ((first, last), human[owner][1] if owner is not None else None)
        for (first, last), owner in pieces
    ]


def _piece_cuts(inside, starts, truth_at):
    cuts = attribution.change_points(inside, truth_at)
    if cuts is not None:
        local = (
            starts[bisect_left(starts, inside[0]) : bisect_right(starts, inside[-1])]
            if len(inside)
            else ()
        )
        cuts = sorted({*cuts, *(bisect_left(inside, t) for t in local)})
    return cuts


def _member_episodes(view, key, group_id):
    """``(episode, result, estimated)`` for one radar, split where the learner switches: a
    span of human room Present takes the human-truth attribution (``room_targets``); the rest
    of an occupied episode takes the full-truth one (``auto_targets``), marked estimated when
    only the estimate made it occupied. Tapping a chip labels exactly its own span."""
    saved = view.data["room_snapshot"]
    human = attribution.radar_attribution(view, key, saved)["found"][group_id]
    full = attribution.radar_attribution(view, key, saved, human=False)
    return _native_episodes(human, full, group_id)


def _native_episodes(human, full, group_id):
    """Split physical intervals at truth ownership edges, preserving their exact ends."""
    read = full["truths"][group_id]
    starts = [span[0] for span, _ in human]
    edges = tuple(truth.edges(full["contexts"][group_id]))
    result = []
    for (start, last), decided in full["found"][group_id]:
        end = nextafter(last, inf)
        cuts = sorted({start, end, *(t for t in edges if start < t < end)})
        for low, high in zip(cuts, cuts[1:], strict=False):
            answer, estimated = _native_owner(read(low), human, starts, low, decided)
            _append_native(result, low, high, answer, estimated)
    return result


def _native_owner(label, human, starts, low, decided):
    owner = attribution._attributed(human, starts, low) if attribution.is_human(label) else None
    answer = _as_learned(owner if owner is not None else decided)
    estimated = owner is None and label is not None and label[1] == "estimate"
    return answer, estimated


def _append_native(result, low, high, answer, estimated):
    piece = ((low, nextafter(high, -inf)), answer, estimated)
    if result and nextafter(result[-1][0][1], inf) == low and result[-1][1:] == (answer, estimated):
        result[-1] = ((result[-1][0][0], piece[0][1]), answer, estimated)
    else:
        result.append(piece)


def _merge(members):
    """Group rows: members' episodes that overlap (or meet across one sample gap) are one row."""
    spans = sorted(
        (
            (span, key, result, estimated)
            for key, items in members.items()
            for span, result, estimated in items
        ),
        key=lambda item: (item[0], item[1]),
    )
    rows = []
    for (start, end), key, result, estimated in spans:
        # One radar's own neighbouring episodes keep separate rows (their results differ).
        if rows and start <= rows[-1]["end"] + attribution.GAP and key not in rows[-1]["devices"]:
            row = rows[-1]
            row["end"] = max(row["end"], end)
            row["devices"][key] = result
            row["estimated"] = row["estimated"] and estimated
        else:
            rows.append(
                {"start": start, "end": end, "estimated": estimated, "devices": {key: result}}
            )
    return rows


@pace.background
def build(views, definitions, group_id):
    """The latest ``EPISODE_LIMIT`` occupied episodes of a group with each member's result
    from ``attribution.radar_attribution`` (what the learner uses). Radars in no multi-radar
    group are not trained from room labels, so they get no chip. ``end`` covers the last
    Present sample's cell, so it can be labelled as a range."""
    members = {
        key: _member_episodes(view, key, group_id)
        for key, view in views.items()
        if view.data.get("room_snapshot") and group_id in view.data["room_snapshot"]["own"]
    }
    rows = _merge(members)[-EPISODE_LIMIT:]
    for row in rows:
        row["end"] = nextafter(row["end"], inf)
        for key in members:
            row["devices"].setdefault(key, dict(UNKNOWN))
    attribution.release(views.values())
    return rows


async def _run(runtime, definitions, group_id, current, view, started):
    try:
        async with runtime._room_attribution_semaphore:
            episodes = await pace.run(runtime.hass, build, view, definitions, group_id)
    except asyncio.CancelledError:
        raise
    except Exception:  # A failing build must not be retried on every poll.
        _LOGGER.exception("Could not attribute occupied periods for %s", group_id)
        episodes = []
    _store(runtime, group_id, current, episodes, started)


def _store(runtime, group_id, current, episodes, started):
    if current["inputs"]["history_hours"] != SEEN_BY_HISTORY_HOURS:
        return  # A later poll rebuilds using the new display policy.
    runtime._room_attribution[group_id] = {
        "signature": current,
        "episodes": episodes,
        "built_at": started,
    }


def _refresh(runtime, definitions, group_id, current, now):
    jobs = runtime._room_attribution_jobs
    if group_id in jobs:
        return  # The next poll after it lands compares signatures again.
    view = _view(runtime, definitions, group_id)
    if view is None:
        _store(runtime, group_id, current, [], now)
        return
    task = runtime.hass.async_create_task(
        _run(runtime, deepcopy(definitions), group_id, current, view, now)
    )
    jobs[group_id] = task
    task.add_done_callback(lambda done: finish_task(jobs, group_id, done))


def _stale(entry, current, now):
    if entry is None or entry["signature"]["inputs"] != current["inputs"]:
        return True
    changed = entry["signature"]["history"] != current["history"]
    return changed and now - entry["built_at"] >= HISTORY_REBUILD_SECONDS


def snapshot(runtime, definitions) -> dict[str, list]:
    """``{group_id: episodes}`` from the cache, starting a rebuild where inputs changed."""
    cache = runtime._room_attribution
    for key in set(cache) - set(definitions):
        cache.pop(key)
    now = _clock()
    result = {}
    for group_id in definitions:
        current = signature(runtime, definitions, group_id)
        entry = cache.get(group_id)
        if _stale(entry, current, now):
            _refresh(runtime, definitions, group_id, current, now)
            entry = cache.get(group_id)
        result[group_id] = entry["episodes"] if entry else []
    return result
