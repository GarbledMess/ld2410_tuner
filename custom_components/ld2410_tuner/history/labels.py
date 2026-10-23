"""Manual training operations on the shared runtime state."""

from __future__ import annotations

import asyncio
import math
import time
from typing import Any

from ..calibration.constants import SAMPLE_SECONDS
from ..const import (
    HISTORY_KEYS,
    HISTORY_LABELS_MAX,
    MAX_HISTOGRAM_COUNT,
    TRAINING_STATES,
)
from ..presence import reference_training, references
from . import daystore, label_counts, spans, summaries
from .policy import retention_seconds


def _compact_history_labels(device: dict[str, Any]) -> None:
    """Merge adjacent/overlapping same-state labels and cap the list.

    Manual and auto-timeout labels accumulate indefinitely otherwise -
    every training-state toggle appends one. Merging same-state runs
    keeps the list bounded without losing any information.
    """
    labels = device.get("history_labels")
    if not labels or len(labels) < 2:
        return
    device["history_labels"] = compact_labels(labels)


def compact_labels(labels: list[dict[str, Any]], key=None) -> list[dict[str, Any]]:
    """Return ``labels`` sorted, with same-``key`` runs merged (default: state) and capped."""
    key = key or (lambda label: label.get("state"))
    labels = sorted(labels, key=lambda x: float(x.get("start", 0)))
    merged: list[dict[str, Any]] = []
    for label in labels:
        start = float(label.get("start", 0))
        end = float(label.get("end", start))
        if merged and key(merged[-1]) == key(label) and start <= float(merged[-1]["end"]):
            merged[-1]["end"] = max(float(merged[-1]["end"]), end)
        else:
            merged.append(dict(label, start=start, end=end))
    if len(merged) > HISTORY_LABELS_MAX:
        merged = merged[-HISTORY_LABELS_MAX:]
    return merged


def apply_range(labels, start, end, state, source="manual"):
    """Return ``labels`` with ``[start, end)`` set to ``state``, keeping outer parts."""
    result = _preserved_labels(labels, start, end)
    result.append({"start": start, "end": end, "state": state, "source": source})
    return result


def replace_label(labels, original_start, original_end, start, end, state):
    """Return ``labels`` with one saved period moved, changed or (unlabelled) removed."""
    # Remove the original boundaries before applying the replacement. Shrinking or
    # moving a period must not silently leave its former label behind.
    result = _preserved_labels(labels, original_start, original_end)
    if state != "unlabelled":
        result = apply_range(result, start, end, state)
    return result


def label_history_range(
    runtime, device_id: str, start: float, end: float, state: str
) -> dict[str, Any]:
    """Apply a retrospective manual label to historical samples in a time range."""
    validate_history_range(start, end, state)
    device = _history_device(runtime, device_id)
    _prepare_history_edit(runtime, device)
    labels = apply_range(device.get("history_labels", []), start, end, state)
    _save_history_edit(runtime, device, labels, [(start, end)])
    seconds = _observed_seconds(runtime, device_id, start, end)
    return {
        "ok": True,
        "labelled_samples": seconds / SAMPLE_SECONDS,  # Recorded time, in six-second units.
        "history_labels": device["history_labels"],
        "observed_seconds": seconds,
        "support_basis": "observed_native_duration",
    }


def edit_history_label(runtime, device_id, label_start, label_end, start, end, state, revision):
    """Replace or remove one saved period, rejecting edits to a stale snapshot."""
    validate_history_range(start, end, "unknown" if state == "unlabelled" else state)
    device = _history_device(runtime, device_id)
    if device.get("label_revision", 0) != revision:
        raise ValueError("History labels changed. Refresh and select the period again.")
    original = next(
        (
            label
            for label in device.get("history_labels", [])
            if label["start"] == label_start and label["end"] == label_end
        ),
        None,
    )
    if original is None:
        raise ValueError("This saved period no longer exists. Refresh and select it again.")
    _prepare_history_edit(runtime, device)
    labels = replace_label(device["history_labels"], label_start, label_end, start, end, state)
    _save_history_edit(runtime, device, labels, [(label_start, label_end), (start, end)])
    return {"ok": True, "history_labels": device["history_labels"]}


def validate_history_range(start, end, state):
    if state not in {"present", "not_present", "unknown"}:
        raise ValueError("Invalid training state")
    if not math.isfinite(start) or not math.isfinite(end) or end <= start:
        raise ValueError("End time must be after start time")


def _history_device(runtime, device_id):
    device = runtime.data.get("devices", {}).get(device_id)
    if not device:
        raise ValueError("Unknown device")
    return device


def _prepare_history_edit(runtime, device):
    runtime._ensure_histograms(device)
    now = time.time()
    runtime._close_training_interval(device, now)
    if device.get("training_state") in {"present", "not_present"}:
        device["training_label_start"] = now


def _save_history_edit(runtime, device, labels, ranges):
    device["history_labels"] = labels
    device["histograms"] = _rebuild_histograms(runtime, device)
    reference_training.correct(runtime, device, ranges)
    device.pop("last_learning", None)
    device["label_revision"] = device.get("label_revision", 0) + 1
    runtime._compact_history_labels(device)
    runtime._schedule_save()


def _observed_seconds(runtime, device_id, start, end):
    """Recorded time in ``[start, end)`` within retention where any gate read."""
    start = max(
        start,
        time.time() - retention_seconds(runtime.data),
        getattr(runtime, "_physical_history_start", -math.inf),
    )
    end = min(end, getattr(runtime, "_attribution_until", math.inf))
    if end <= start:
        return 0.0
    days = getattr(runtime, "_days", None)
    if days is None:
        return 0.0
    return sum(
        summary.observed(start, end)
        for date in days.days(device_id)
        if daystore.bounds(date)[1] > start and daystore.bounds(date)[0] < end
        if (summary := summaries.of(runtime, device_id, date)) is not None
    )


def _manual_history_state(device: dict[str, Any], timestamp: float) -> str | None:
    active_start = device.get("training_label_start")
    expires = device.get("training_expires_at")
    if (
        active_start is not None
        and timestamp >= active_start
        and (not expires or timestamp < expires)
    ):
        return device.get("training_state", "unknown")
    # The radar's own periods win over its lone groups' copies (rooms/lone_labels.py).
    for labels in (device.get("history_labels", []), device.get("group_labels", [])):
        for label in reversed(labels):
            if float(label.get("start", 0)) <= timestamp < float(label.get("end", 0)):
                return label.get("state", "unknown")
    return None


def _history_label_at(device: dict[str, Any], timestamp: float) -> str:
    return _manual_history_state(device, timestamp) or "unknown"


def _compress_histogram(histogram: list[int]) -> None:
    total = sum(histogram)
    if total <= MAX_HISTOGRAM_COUNT:
        return
    # Keep a bounded, recency-weighted distribution. Halving makes recent
    # observations progressively more important without storing raw samples.
    fractional = any(isinstance(count, float) for count in histogram)
    if fractional:
        histogram[:] = [value * MAX_HISTOGRAM_COUNT / total for value in histogram]
        return
    for i, value in enumerate(histogram):
        histogram[i] = value // 2


def _ensure_histograms(runtime, device: dict[str, Any]) -> dict[str, dict[str, list[int]]]:
    return device.setdefault("histograms", {})


def restore_timeouts(runtime) -> None:
    for device_id, device in runtime.data.get("devices", {}).items():
        expires_at = device.get("training_expires_at")
        if expires_at and device.get("training_state") in {"present", "not_present"}:
            runtime._schedule_timeout(device_id, max(0, float(expires_at) - time.time()))


def set_training_state(
    runtime, device_id: str, state: str, timeout_seconds: int | None = None
) -> None:
    device = _change_training_state(runtime, device_id, state, timeout_seconds)
    _native_label_rebuild(runtime, device_id, device)
    runtime._schedule_save()


def _change_training_state(runtime, device_id, state, timeout_seconds):
    if state not in TRAINING_STATES:
        raise ValueError("Invalid training state")
    device = runtime.data["devices"].get(device_id)
    if not device:
        raise ValueError("Unknown device")
    if state != "unknown" and not device.get("recording_enabled", True):
        raise ValueError("Enable recording before setting the current presence label")
    old_task = runtime._timeout_tasks.pop(device_id, None)
    if old_task:
        old_task.cancel()

    now = time.time()
    runtime._close_training_interval(device, now)
    device["training_state"] = state
    device["label_revision"] = device.get("label_revision", 0) + 1
    if state in {"present", "not_present"}:
        device["training_label_start"] = now
    if state in {"present", "not_present"} and timeout_seconds:
        timeout_seconds = max(1, int(timeout_seconds))
        device["training_timeout_seconds"] = timeout_seconds
        device["training_expires_at"] = time.time() + timeout_seconds
        runtime._schedule_timeout(device_id, timeout_seconds)
    else:
        device.pop("training_timeout_seconds", None)
        device.pop("training_expires_at", None)
    return device


def _native_label_rebuild(runtime, device_id, device):
    if not spans.has_events(runtime, device_id):
        return
    if device_id not in runtime._label_counts:
        # Counting all history here would stall the event loop (seconds per week of
        # history); count in the executor, then rebuild from the kept counts.
        _rebuild_when_counted(runtime, device_id)
        return
    runtime._ensure_histograms(device)
    device["histograms"] = _rebuild_histograms(runtime, device)


def _rebuild_when_counted(runtime, device_id):
    """One pending rebuild per radar: it reads the labels as they are once counted."""
    if device_id in runtime._label_rebuilds:
        return

    async def rebuild():
        from ..worker.supervisor import Busy, Closed

        try:
            await label_counts.ensure(runtime, device_id)
        except (Busy, Closed):
            return  # The next label change or cleanup rebuilds them.
        finally:
            if runtime._label_rebuilds.get(device_id) is asyncio.current_task():
                runtime._label_rebuilds.pop(device_id, None)
        _rebuild_counted(runtime, device_id)

    runtime._label_rebuilds[device_id] = runtime.hass.async_create_background_task(
        rebuild(), f"ld2410_tuner label counts {device_id}"
    )


def _rebuild_counted(runtime, device_id):
    device = runtime.data["devices"].get(device_id)
    # Never count all history here: without kept counts, the next change retries.
    if device is None or device_id not in runtime._label_counts:
        return
    if spans.has_events(runtime, device_id):
        runtime._ensure_histograms(device)
        device["histograms"] = _rebuild_histograms(runtime, device)
        runtime._schedule_save()


def _close_training_interval(runtime, device, now):
    active_start = device.pop("training_label_start", None)
    state = device.get("training_state")
    end = min(now, device.get("training_expires_at") or now)
    if state in {"present", "not_present"} and active_start is not None and end > active_start:
        device.setdefault("history_labels", []).append(
            {"start": active_start, "end": end, "state": state, "source": "live"}
        )
        runtime._compact_history_labels(device)


def _schedule_timeout(runtime, device_id: str, seconds: float) -> None:
    old_task = runtime._timeout_tasks.pop(device_id, None)
    if old_task:
        old_task.cancel()
    runtime._timeout_tasks[device_id] = runtime.hass.async_create_task(
        runtime._timeout_worker(device_id, seconds)
    )


async def _timeout_worker(runtime, device_id: str, seconds: float) -> None:
    try:
        await asyncio.sleep(seconds)
        device = runtime.data.get("devices", {}).get(device_id)
        if (
            device
            and device.get("training_expires_at")
            and device["training_expires_at"] <= time.time()
        ):
            runtime._close_training_interval(device, time.time())
            device["training_state"] = "unknown"
            device.pop("training_label_start", None)
            device.pop("training_expires_at", None)
            _native_label_rebuild(runtime, device_id, device)
            runtime._schedule_save()
    finally:
        if runtime._timeout_tasks.get(device_id) is asyncio.current_task():
            runtime._timeout_tasks.pop(device_id, None)


def clear_samples(runtime, device_id: str) -> None:

    if device_id in runtime.data["devices"]:
        # No histogram rebuild: they are emptied below with the history they count.
        _change_training_state(runtime, device_id, "unknown", None)
        pending = runtime._label_rebuilds.pop(device_id, None)
        if pending is not None:
            pending.cancel()
        runtime.data["devices"][device_id].pop("last_learning", None)
        runtime.data["devices"][device_id].pop("learning_results", None)
        runtime.data["devices"][device_id].pop("comparison_scores", None)
        runtime._comparison_cache.pop(device_id, None)
        runtime.data["devices"][device_id].pop("nightly_learning", None)
        if device_id not in runtime._learning_jobs:
            runtime.data["devices"][device_id].pop("learning_job", None)
        runtime.data["devices"][device_id]["label_revision"] = (
            runtime.data["devices"][device_id].get("label_revision", 0) + 1
        )
        runtime.data["devices"][device_id]["histograms"] = {}
        runtime.data["devices"][device_id].pop("auto", None)
        runtime.data["devices"][device_id].pop("reference_profile", None)
        runtime.data["devices"][device_id].pop(references.STALE, None)
        runtime.data["devices"][device_id].pop("history_labels", None)
        runtime._live.pop(device_id, None)
        runtime._auto_runtime.pop(device_id, None)
        runtime._source_runtime.pop(device_id, None)
        runtime._history_runtime.pop(device_id, None)
        if runtime._days is not None:
            from . import commit

            commit.clear_device(runtime, device_id)
        label_counts.forget(runtime, device_id)
        runtime._schedule_save()


def _preserved_labels(labels, start, end):
    preserved = []
    for existing in labels:
        old_start = float(existing.get("start", 0))
        old_end = float(existing.get("end", 0))
        if old_end <= start or old_start >= end:
            preserved.append(existing)
            continue
        # Preserve portions of an existing labelled interval outside the
        # newly edited range, so a correction does not erase neighbouring
        # training data.
        if old_start < start:
            left = dict(existing)
            left["end"] = start
            preserved.append(left)
        if old_end > end:
            right = dict(existing)
            right["start"] = end
            preserved.append(right)
    return preserved


def _rebuild_histograms(runtime, device):
    """Every retained recorded second's gate values under its current label, as stored
    (``label_counts.stored``). The counting itself is ``label_counts`` (incremental)."""
    counted = label_counts.current(runtime, device)
    return label_counts.stored({key: counted[key] for key in HISTORY_KEYS})
