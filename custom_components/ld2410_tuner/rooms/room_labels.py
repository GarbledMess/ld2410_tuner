"""Human occupancy labels stored per room group, reusing the device label helpers."""

from __future__ import annotations

import math
import time
from typing import Any

from ..calibration.constants import SAMPLE_SECONDS
from ..history import guesses, recording
from ..history.labels import (
    apply_range,
    compact_labels,
    replace_label,
    validate_history_range,
)
from . import groups, membership, truth

FEEDBACK_WINDOW_SECONDS = 15
# Longest stretch "Was this right?" labels: one hour back from now.
FEEDBACK_MAX_SECONDS = 3600
# The published estimate is reused within this bucket unless members record more.
ESTIMATE_CACHE_SECONDS = 5
NO_ESTIMATE = "No confident room estimate to give feedback on yet"
STALE_MESSAGE = "History labels changed. Refresh and select the period again."


def room_labels_for(runtime, group_id: str) -> list[dict[str, Any]]:
    return list(runtime.data.get("room_labels", {}).get(group_id, []))


def label_room_range(runtime, group_id, start, end, state) -> dict[str, Any]:
    """Label a period of a room (source "manual"), keeping neighbouring labels."""
    validate_history_range(start, end, state)
    _require_group(runtime, group_id)
    others, manual = _split(runtime, group_id)
    return _save(runtime, group_id, others, apply_range(manual, start, end, state, "manual"))


def room_estimate_feedback(runtime, group_id, correct: bool) -> dict[str, Any]:
    """ "Was this right?" on the room estimate: write a human label for its stretch.

    The server finds the stretch (``current_estimate``), so the browser clock and what the
    panel happened to see play no part. Bounded like device feedback: the estimate must be
    confident and recorded within the last ``FEEDBACK_WINDOW_SECONDS``.
    """
    _require_group(runtime, group_id)
    estimate = current_estimate(runtime, groups.definitions(runtime)[2][group_id]["device_ids"])
    if estimate is None:
        raise ValueError(NO_ESTIMATE)
    state = estimate["state"]
    opposite = "not_present" if state == "present" else "present"
    return label_room_range(
        runtime, group_id, estimate["start"], estimate["end"], state if correct else opposite
    )


def _recent(runtime, key, since):
    """``(newest, runs)`` of a member: the time of its newest recorded sample since ``since``
    (None for none) and its guess runs ending since then; a member missing from the registry
    has neither. Kept while the member records nothing new (``_history_mark``), so a poll that
    finds one new sample reads that sample and the runs it ends, not the last hour."""
    devices = runtime.data["devices"]
    if devices.get(key) is None:
        return None, guesses.EMPTY
    kept = getattr(runtime, "_room_recent", None)
    mark = _history_mark(runtime, devices, key)
    known = kept.get(key) if kept is not None else None
    if known is None or known["mark"] != mark:
        known = {"mark": mark, "since": None, "runs": guesses.EMPTY}
        if kept is not None:
            kept[key] = known
    # Recorded history observed up to now moves without a new record (open coverage).
    newest = recording.newest(runtime, key, since)
    if known["since"] is None or known["since"] > since or _went_back(known, newest):
        known["since"], known["runs"] = since, guesses.evidence(runtime, key, since)
        known["read"] = newest
    if newest is None:
        return None, known["runs"]
    return newest, _continued(known, newest)


def _went_back(known, newest):
    return newest is not None and known.get("read") is not None and newest < known["read"]


def _continued(known, newest):
    """The kept runs as of ``newest``: with no new record since they were read, the run that
    was open then is still open, so it reaches ``newest``."""
    runs, read = known["runs"].runs, known.get("read")
    if read is None or newest == read or not runs or runs[-1][1] != read:
        return known["runs"]
    return guesses.Guesses([*runs[:-1], (runs[-1][0], newest, *runs[-1][2:])])


def current_estimate(runtime, device_ids) -> dict[str, Any] | None:
    """The latest recorded room estimate and its stretch, or None when it is not current.

    Reads at most ``FEEDBACK_MAX_SECONDS`` of each member's guess runs (only recent chunks are
    decoded) and its newest sample, so it is cheap enough for every snapshot. The stretch walks
    back from the latest sample while the estimate between neighbouring samples keeps one
    confident state, and stops at a recording gap.
    """
    now = time.time()
    since = now - FEEDBACK_MAX_SECONDS
    found = {key: _recent(runtime, key, since) for key in device_ids}
    newest = {key: sample for key, (sample, _runs) in found.items()}
    runs = {key: item for key, (_sample, item) in found.items()}
    cells = {
        key: [(max(a, since), b) for a, b, _ in item.cells(since)] for key, item in runs.items()
    }
    read = truth.estimate_reader({key: {"automatic": item} for key, item in runs.items()})
    latest = max((sample for sample in newest.values() if sample is not None), default=None)
    anchor = _anchor(newest, cells)
    # Anchored at the newest samples: only a member whose newest sample IS a confident guess
    # keeps the estimate current, within one sample of the newest row (members record
    # asynchronously). Once every member's newest sample is unguessed or below the floor,
    # there is no current estimate.
    if anchor is None or latest - anchor > SAMPLE_SECONDS:
        return None
    if now - anchor > FEEDBACK_WINDOW_SECONDS:
        return None
    edges = sorted(
        {edge for items in cells.values() for span in items for edge in span if edge <= anchor}
    )
    return _stretch(read, edges, anchor)


def _anchor(newest, cells):
    """The last estimated sample: the newest sample of a member whose newest sample is a
    confident guess, or None. A cell ends at its own sample when it is the member's newest
    (recorded cells are clipped there), so that sample is covered by its last cell."""
    anchors = [
        sample
        for key, sample in newest.items()
        if sample is not None and cells[key] and sample <= cells[key][-1][1]
    ]
    return max(anchors, default=None)


def _stretch(read, edges, anchor):
    """Walk back over physical cell edges while one confident state holds. The estimate
    ends at the exact observed anchor, without extending beyond recorded coverage."""
    state, start = None, None
    for before, after in zip(reversed(edges[:-1]), reversed(edges[1:]), strict=True):
        # A recording gap has no cell between its samples, so it reads as None.
        label = read((before + after) / 2)
        if label is None or (state and label[0] != state[0]):
            break
        state, start = state or label, before
    if state is None:
        return None
    return {"state": state[0], "confidence": state[2], "start": start, "end": anchor}


def _history_mark(runtime, devices, key):
    if key not in devices:
        return (key,)
    # A label edit rebuilds the histograms that decide where present guesses were quiet.
    labels = devices[key].get("label_revision", 0)
    return key, labels, recording.history_mark(runtime, key)


def cached_estimate(runtime, group_id, device_ids):
    """``current_estimate`` for the snapshot, recomputed only when a member records or its
    labels change, or the ``ESTIMATE_CACHE_SECONDS`` bucket turns (staleness)."""
    devices = runtime.data["devices"]
    key = (
        int(time.time() // ESTIMATE_CACHE_SECONDS),
        tuple(_history_mark(runtime, devices, member) for member in device_ids),
        # Open coverage is observed up to now: the estimate's anchor moves with it.
        tuple(recording.newest(runtime, member, -math.inf) for member in device_ids),
    )
    entry = runtime._room_estimates.get(group_id)
    if entry is None or entry[0] != key:
        entry = (key, current_estimate(runtime, device_ids))
        runtime._room_estimates[group_id] = entry
    estimate = entry[1]
    # A closed session expires from its exact observed end, even within a cache bucket.
    if estimate and time.time() - estimate["end"] > FEEDBACK_WINDOW_SECONDS:
        return None
    return estimate


def edit_room_label(
    runtime, group_id, label_start, label_end, start, end, state, revision
) -> dict[str, Any]:
    """Replace or remove one saved room period, rejecting edits to a stale snapshot."""
    validate_history_range(start, end, "unknown" if state == "unlabelled" else state)
    _require_group(runtime, group_id)
    if runtime.data.get("room_label_revision", {}).get(group_id, 0) != revision:
        raise ValueError(STALE_MESSAGE)
    others, manual = _split(runtime, group_id)
    if not any(x["start"] == label_start and x["end"] == label_end for x in manual):
        raise ValueError("This saved period no longer exists. Refresh and select it again.")
    labels = replace_label(manual, label_start, label_end, start, end, state)
    return _save(runtime, group_id, others, labels)


def _split(runtime, group_id):
    """Separate non-manual (sensor) labels, which manual edits must never touch."""
    labels = room_labels_for(runtime, group_id)
    manual = [x for x in labels if x.get("source") == "manual"]
    return [x for x in labels if x.get("source") != "manual"], manual


def _require_group(runtime, group_id) -> None:
    if group_id not in groups.definitions(runtime)[2]:
        raise ValueError("This group no longer exists; refresh the panel")


def write_sensor_range(runtime, group_id, start, end, state, confidence, bump=True) -> None:
    """Record a source-derived stretch (source "sensor") without touching any other label.

    Only the sensor subset is rewritten, so manual labels coexist and may overlap. Contiguous
    stretches of the same state and confidence merge, which makes an open label cheap to
    extend. Mark assessments stale but leave reference retraining to the caller, which defers
    it until the label closes (see ``rooms/room_sources.py``). ``bump`` advances
    ``room_sensor_revision`` (assessment staleness only); pure extension passes False.
    """
    others = [x for x in room_labels_for(runtime, group_id) if x.get("source") != "sensor"]
    sensor = [x for x in room_labels_for(runtime, group_id) if x.get("source") == "sensor"]
    sensor = apply_range(sensor, start, end, state, "sensor")
    sensor[-1]["confidence"] = confidence
    sensor = compact_labels(sensor, _label_key)
    labels = sorted(others + sensor, key=lambda x: float(x.get("start", 0)))
    runtime.data.setdefault("room_labels", {})[group_id] = labels
    if bump:
        bump_sensor_revision(runtime, group_id)
    runtime._schedule_save()


def bump_sensor_revision(runtime, group_id) -> None:
    """Sensor-only counter: part of assessment signatures, never checked by manual edits."""
    revisions = runtime.data.setdefault("room_sensor_revision", {})
    revisions[group_id] = revisions.get(group_id, 0) + 1


def _label_key(label):
    return label.get("state"), label.get("source"), label.get("confidence")


def _store(runtime, group_id, labels) -> int:
    revisions = runtime.data.setdefault("room_label_revision", {})
    revisions[group_id] = revisions.get(group_id, 0) + 1
    runtime.data.setdefault("room_labels", {})[group_id] = labels
    runtime._schedule_save()
    return revisions[group_id]


def _save(runtime, group_id, others, manual) -> dict[str, Any]:
    # Compact and cap the manual subset alone: sensor volume must never evict human labels.
    labels = sorted(
        others + compact_labels(manual, _label_key), key=lambda x: float(x.get("start", 0))
    )
    revision = _store(runtime, group_id, labels)
    membership.labels_changed(runtime, group_id)
    # Assessment signatures include the label revision (rooms/jobs.py), so the
    # saved report reads as stale until the room is assessed again.
    return {"ok": True, "room_labels": labels, "revision": revision}
