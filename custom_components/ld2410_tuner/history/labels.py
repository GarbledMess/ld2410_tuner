"""Manual training operations on the shared runtime state."""

from __future__ import annotations

import asyncio
import heapq
import math
import time
from bisect import bisect_right
from collections import defaultdict
from copy import deepcopy
from typing import Any

from ..const import (
    HISTOGRAM_BINS,
    HISTORY_KEYS,
    HISTORY_LABELS_MAX,
    MAX_HISTOGRAM_COUNT,
    TRAINING_STATES,
)


def _compact_history_labels(device: dict[str, Any]) -> None:
    """Merge adjacent/overlapping same-state labels and cap the list.

    Manual and auto-timeout labels accumulate indefinitely otherwise -
    every training-state toggle appends one. Merging same-state runs
    keeps the list bounded without losing any information.
    """
    labels = device.get("history_labels")
    if not labels or len(labels) < 2:
        return
    labels = sorted(labels, key=lambda x: float(x.get("start", 0)))
    merged: list[dict[str, Any]] = []
    for label in labels:
        start = float(label.get("start", 0))
        end = float(label.get("end", start))
        if (
            merged
            and merged[-1].get("state") == label.get("state")
            and start <= float(merged[-1]["end"])
        ):
            merged[-1]["end"] = max(float(merged[-1]["end"]), end)
        else:
            merged.append(dict(label, start=start, end=end))
    if len(merged) > HISTORY_LABELS_MAX:
        merged = merged[-HISTORY_LABELS_MAX:]
    device["history_labels"] = merged


def label_history_range(
    runtime, device_id: str, start: float, end: float, state: str
) -> dict[str, Any]:
    """Apply a retrospective manual label to historical samples in a time range."""
    _validate_history_range(start, end, state)
    device = _history_device(runtime, device_id)
    _prepare_history_edit(runtime, device)
    labels = _preserved_labels(device.get("history_labels", []), start, end)
    labels.append({"start": start, "end": end, "state": state, "source": "manual"})
    _save_history_edit(runtime, device, labels)
    return {
        "ok": True,
        "labelled_samples": sum(
            1 for ts, _ in runtime._iter_history_samples(device) if start <= ts < end
        ),
        "history_labels": device["history_labels"],
    }


def edit_history_label(runtime, device_id, label_start, label_end, start, end, state, revision):
    """Replace or remove one saved period, rejecting edits to a stale snapshot."""
    _validate_history_range(start, end, "unknown" if state == "unlabelled" else state)
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
    # Remove the original boundaries before applying the replacement. Shrinking or
    # moving a period must not silently leave its former label behind.
    labels = _preserved_labels(device["history_labels"], label_start, label_end)
    if state != "unlabelled":
        labels = _preserved_labels(labels, start, end)
        labels.append({"start": start, "end": end, "state": state, "source": "manual"})
    _save_history_edit(runtime, device, labels)
    return {"ok": True, "history_labels": device["history_labels"]}


def _validate_history_range(start, end, state):
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
    device.setdefault("history_legacy_histograms", deepcopy(device["histograms"]))
    now = time.time()
    runtime._close_training_interval(device, now)
    if device.get("training_state") in {"present", "not_present"}:
        device["training_label_start"] = now


def _save_history_edit(runtime, device, labels):
    device["history_labels"] = labels
    device["histograms"] = _rebuild_histograms(runtime, device)
    device.pop("last_learning", None)
    device["label_revision"] = device.get("label_revision", 0) + 1
    runtime._compact_history_labels(device)
    runtime._schedule_save()


def _manual_history_state(device: dict[str, Any], timestamp: float) -> str | None:
    active_start = device.get("training_label_start")
    expires = device.get("training_expires_at")
    if (
        active_start is not None
        and timestamp >= active_start
        and (not expires or timestamp < expires)
    ):
        return device.get("training_state", "unknown")
    for label in reversed(device.get("history_labels", [])):
        if float(label.get("start", 0)) <= timestamp < float(label.get("end", 0)):
            return label.get("state", "unknown")
    return None


def _history_label_at(device: dict[str, Any], timestamp: float) -> str:
    return _manual_history_state(device, timestamp) or "unknown"


def _migrate_device_samples(runtime, device: dict[str, Any]) -> None:
    """Convert the old raw sample arrays to bounded histograms once."""
    if "histograms" in device:
        return
    histograms: dict[str, dict[str, list[int]]] = {}
    for key, series in device.pop("samples", {}).items():
        out = {"present": [0] * HISTOGRAM_BINS, "not_present": [0] * HISTOGRAM_BINS}
        for state in ("present", "not_present"):
            out[state] = _legacy_histogram(series.get(state, []))
            runtime._compress_histogram(out[state])
        histograms[key] = out
    device["histograms"] = histograms


def _compress_histogram(histogram: list[int]) -> None:
    total = sum(histogram)
    if total <= MAX_HISTOGRAM_COUNT:
        return
    # Keep a bounded, recency-weighted distribution. Halving makes recent
    # observations progressively more important without storing raw samples.
    for i, value in enumerate(histogram):
        histogram[i] = value // 2


def _ensure_histograms(runtime, device: dict[str, Any]) -> dict[str, dict[str, list[int]]]:
    runtime._migrate_device_samples(device)
    return device.setdefault("histograms", {})


def restore_timeouts(runtime) -> None:
    for device_id, device in runtime.data.get("devices", {}).items():
        expires_at = device.get("training_expires_at")
        if expires_at and device.get("training_state") in {"present", "not_present"}:
            runtime._schedule_timeout(device_id, max(0, float(expires_at) - time.time()))


def set_training_state(
    runtime, device_id: str, state: str, timeout_seconds: int | None = None
) -> None:
    if state not in TRAINING_STATES:
        raise ValueError("Invalid training state")
    device = runtime.data["devices"].get(device_id)
    if not device:
        raise ValueError("Unknown device")
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
            runtime._schedule_save()
    finally:
        if runtime._timeout_tasks.get(device_id) is asyncio.current_task():
            runtime._timeout_tasks.pop(device_id, None)


def clear_samples(runtime, device_id: str) -> None:
    if device_id in runtime.data["devices"]:
        runtime.set_training_state(device_id, "unknown")
        runtime.data["devices"][device_id].pop("last_learning", None)
        runtime.data["devices"][device_id]["label_revision"] = (
            runtime.data["devices"][device_id].get("label_revision", 0) + 1
        )
        runtime.data["devices"][device_id]["histograms"] = {}
        runtime.data["devices"][device_id].pop("auto", None)
        runtime.data["devices"][device_id].pop("history", None)
        runtime.data["devices"][device_id].pop("history_labels", None)
        runtime.data["devices"][device_id].pop("history_legacy_histograms", None)
        runtime._live.pop(device_id, None)
        runtime._auto_runtime.pop(device_id, None)
        runtime._history_runtime.pop(device_id, None)
        runtime._schedule_save()


def _history_label_reader(device):
    """Resolve labels once, keeping last-label-wins semantics in logarithmic time."""
    intervals = list(device.get("history_labels", []))
    start = device.get("training_label_start")
    if start is not None:
        intervals.append(
            {
                "start": start,
                "end": device.get("training_expires_at") or float("inf"),
                "state": device.get("training_state", "unknown"),
            }
        )
    events = _label_events(intervals)
    times, states = _label_index(events)

    def read(timestamp):
        index = bisect_right(times, timestamp) - 1
        return states[index] if index >= 0 else None

    return read


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
    rebuilt = {
        key: {"present": [0] * HISTOGRAM_BINS, "not_present": [0] * HISTOGRAM_BINS}
        for key in HISTORY_KEYS
    }
    label_at = _history_label_reader(device)
    for timestamp, row in runtime._iter_history_samples(device):
        label = label_at(timestamp)
        if label not in {"present", "not_present"}:
            continue
        for index, key in enumerate(HISTORY_KEYS):
            value = row[index]
            if value <= 100:
                rebuilt[key][label][value] += 1
    _restore_legacy_histograms(device, rebuilt)
    for series in rebuilt.values():
        runtime._compress_histogram(series["present"])
        runtime._compress_histogram(series["not_present"])
    return rebuilt


def _restore_legacy_histograms(device, rebuilt):
    legacy = device.get("history_legacy_histograms", {})
    for key, legacy_series in legacy.items():
        target = rebuilt.setdefault(
            key, {"present": [0] * HISTOGRAM_BINS, "not_present": [0] * HISTOGRAM_BINS}
        )
        for label in ("present", "not_present"):
            old_hist = legacy_series.get(label, [])
            for index, count in enumerate(old_hist[:HISTOGRAM_BINS]):
                target[label][index] += int(count)


def _label_events(intervals):
    events = defaultdict(list)
    for order, label in enumerate(intervals):
        start, end = float(label.get("start", 0)), float(label.get("end", 0))
        if end > start:
            events[start].append((-order, end, label.get("state", "unknown")))
            events[end]  # Endpoints are needed even when no new interval starts.
    return events


def _label_index(events):
    times, states, active = [], [], []
    for timestamp, additions in sorted(events.items()):
        for entry in additions:
            heapq.heappush(active, entry)
        while active and active[0][1] <= timestamp:
            heapq.heappop(active)
        times.append(timestamp)
        states.append(active[0][2] if active else None)
    return times, states


def _legacy_histogram(values):
    histogram = [0] * HISTOGRAM_BINS
    for value in values:
        try:
            value = max(0, min(100, int(round(float(value)))))
        except (TypeError, ValueError):
            continue
        histogram[value] += 1
    return histogram
