"""Recording operations on the shared runtime state."""

from __future__ import annotations

import asyncio
import struct
import time
import zlib
from copy import deepcopy
from functools import lru_cache
from typing import Any

from homeassistant.core import callback

from ..calibration.fitting import METHOD
from ..const import (
    AUTO_SAMPLE_INTERVAL,
    HISTOGRAM_BINS,
    HISTORY_BLOCK_SAMPLES,
    HISTORY_KEYS,
    HISTORY_RETENTION_SECONDS,
    HISTORY_SAMPLE_INTERVAL,
)
from .cleanup import clean_history, decode_payload, encode_payload


def _record_history_sample(runtime, device_id: str, values: dict[str, float], now: float) -> None:
    """Keep a compact time-indexed history so past periods can be labelled later."""
    state = runtime._history_runtime.setdefault(
        device_id, {"last_sample": 0.0, "samples": [], "start": None}
    )
    device = runtime.data.get("devices", {}).get(device_id)
    if device is None:
        return
    if "history_legacy_histograms" not in device:
        device["history_legacy_histograms"] = deepcopy(device.get("histograms", {}))
    if now - float(state.get("last_sample", 0.0)) < HISTORY_SAMPLE_INTERVAL:
        return
    state["last_sample"] = now
    if state.get("start") is None:
        state["start"] = now
    row = bytes(
        max(0, min(100, int(round(values.get(key, 255))))) if key in values else 255
        for key in HISTORY_KEYS
    )
    if state["samples"] and now - state["samples"][0][0] > 65535:
        runtime._flush_history_block(device_id)
    inferred, code, confidence = _stored_estimate(device, now)
    state["samples"].append((now, row + bytes((code, confidence))))
    if code:
        observations = device.setdefault("auto", {}).setdefault("observations", {})
        observations[inferred] = observations.get(inferred, 0) + 1
    _record_manual_histogram(runtime, device, row, now)
    if len(state["samples"]) >= HISTORY_BLOCK_SAMPLES:
        runtime._flush_history_block(device_id)


def _flush_history_block(runtime, device_id: str) -> None:
    state = runtime._history_runtime.get(device_id)
    if not state or not state.get("samples"):
        return
    samples = state["samples"]
    start = float(samples[0][0])
    raw = bytearray()
    for timestamp, row in samples:
        offset = max(0, min(65535, int(round(timestamp - start))))
        raw.extend(struct.pack(">H", offset))
        raw.extend(row[: len(HISTORY_KEYS)])
        raw.extend(
            row[len(HISTORY_KEYS) : len(HISTORY_KEYS) + 2]
            if len(row) >= len(HISTORY_KEYS) + 2
            else bytes(2)
        )
    block = {
        "version": 3,
        "start": start,
        "count": len(samples),
        "end": float(samples[-1][0]),
        "data": encode_payload(bytes(raw)),
    }
    device = runtime.data.get("devices", {}).get(device_id)
    if device is not None:
        history = device.setdefault("history", [])
        history.append(block)
        cutoff = time.time() - HISTORY_RETENTION_SECONDS
        device["history"] = [
            b for b in history if float(b.get("end", float(b.get("start", 0)) + 65535)) >= cutoff
        ]
    state["samples"] = []
    state["start"] = None


@callback
def _enforce_history_retention(runtime, *_args) -> None:
    if runtime._cleanup_task is None or runtime._cleanup_task.done():
        runtime._cleanup_task = runtime.hass.async_create_task(runtime.async_clean_history())


async def async_clean_history(runtime, *, persist=True):
    """Normalize stored history at startup and hourly, without racing writes."""
    changed = False
    changed = await _clean_devices(runtime, changed)
    if changed:
        runtime._history_cache.clear()
        if persist:
            runtime._schedule_save()
    return changed


async def flush_history(runtime) -> None:
    for device_id in runtime._history_runtime:
        runtime._flush_history_block(device_id)
    runtime._schedule_save()
    if runtime._save_task:
        await asyncio.shield(runtime._save_task)


def _iter_history_samples(runtime, device: dict[str, Any], since=0, include_auto=False):
    cutoff = max(since, time.time() - HISTORY_RETENTION_SECONDS)
    yield from _stored_history_samples(device, cutoff, include_auto)

    yield from _pending_history_samples(runtime, device, cutoff, include_auto)


def _history_view(runtime, device_id):
    from ..runtime.coordinator import TunerRuntime

    device = runtime.data["devices"].get(device_id)
    if not device:
        raise ValueError("Unknown device")
    snapshot = {
        key: device.get(key)
        for key in ("training_state", "training_label_start", "training_expires_at")
    }
    snapshot["history"] = list(device.get("history", []))
    snapshot["history_labels"] = deepcopy(device.get("history_labels", []))
    view = TunerRuntime(None, None, {"devices": {device_id: snapshot}})
    view._history_runtime[device_id] = {
        "samples": list(runtime._history_runtime.get(device_id, {}).get("samples", []))
    }
    return view


@lru_cache(maxsize=1024)
def _decode_history_block(encoded, version, count):
    return decode_payload(encoded, version, count)


def _record_manual_histogram(runtime, device, row, now):
    label = runtime._history_label_at(device, now)
    if label in {"present", "not_present"}:
        for index, key in enumerate(HISTORY_KEYS):
            if row[index] > 100:
                continue
            series = device.setdefault("histograms", {}).setdefault(
                key, {"present": [0] * HISTOGRAM_BINS, "not_present": [0] * HISTOGRAM_BINS}
            )
            series[label][row[index]] += 1
            runtime._compress_histogram(series[label])


def _stored_history_samples(device, cutoff, include_auto):
    for block in device.get("history", []):
        try:
            yield from _block_samples(block, cutoff, include_auto)
        except (KeyError, TypeError, ValueError, zlib.error, struct.error):
            continue


def _block_samples(block, cutoff, include_auto):
    start = float(block["start"])
    if float(block.get("end", start + 65535)) < cutoff:
        return
    count, version = int(block["count"]), block.get("version", 1)
    raw = _decode_history_block(block["data"], version, count)
    if version not in (1, 2, 3):
        return
    stride = 2 + len(HISTORY_KEYS) + (2 if version >= 2 else 0)
    if len(raw) < count * stride:
        return
    for index in range(count):
        pos = index * stride
        timestamp = start + struct.unpack(">H", raw[pos : pos + 2])[0]
        if timestamp >= cutoff:
            values = raw[pos + 2 : pos + stride]
            yield timestamp, values if include_auto else values[: len(HISTORY_KEYS)]


def _pending_history_samples(runtime, device, cutoff, include_auto):
    device_id = next(
        (key for key, candidate in runtime.data["devices"].items() if candidate is device), None
    )
    samples = runtime._history_runtime.get(device_id, {}).get("samples", [])
    for timestamp, row in samples:
        if timestamp >= cutoff:
            yield timestamp, row if include_auto else row[: len(HISTORY_KEYS)]


async def _clean_devices(runtime, changed):
    # Registry discovery can change the device dictionary while cleanup awaits its executor.
    devices = runtime.data.get("devices", {}).copy()
    for device_id, device in devices.items():
        snapshot = {
            key: deepcopy(device[key])
            for key in (
                "history",
                "history_labels",
                "histograms",
                "history_legacy_histograms",
                "training_state",
                "training_label_start",
                "training_expires_at",
            )
            if key in device
        }
        pending = list(runtime._history_runtime.get(device_id, {}).get("samples", []))
        revision = device.get("label_revision", 0)
        updated, stats = await runtime.hass.async_add_executor_job(
            clean_history, snapshot, pending, time.time(), HISTORY_RETENTION_SECONDS
        )
        # Sampling, Clear or a human correction may have run in the meantime.
        # Leave their new data intact; the next maintenance pass retries.
        if (
            runtime.data.get("devices", {}).get(device_id) is not device
            or device.get("label_revision", 0) != revision
            or any(device.get(key) != snapshot.get(key) for key in snapshot)
            or runtime._history_runtime.get(device_id, {}).get("samples", []) != pending
        ):
            continue
        if any(device.get(key) != value for key, value in updated.items()):
            _accept_cleanup(device, updated, stats, revision)
            changed = True
    return changed


def _stored_estimate(device, now):
    last = device.get("auto", {}).get("last_classification", {})
    inferred = (
        last.get("state")
        if now - last.get("timestamp", 0) <= AUTO_SAMPLE_INTERVAL * 2
        else "unknown"
    )
    code = {"present": 1, "not_present": 2}.get(inferred, 0)
    confidence = max(0, min(100, round(last.get("confidence", 0) * 100))) if code else 0
    return inferred, code, confidence


def _accept_cleanup(device, updated, stats, revision):
    device.update(updated)
    device["history_cleanup"] = stats
    if any(
        stats[key]
        for key in (
            "invalid_blocks",
            "expired_samples",
            "duplicate_samples",
            "repaired_samples",
            "discarded_samples",
        )
    ):
        device["label_revision"] = revision + 1
        device.pop("last_learning", None)
    elif (device.get("last_learning") or {}).get("method") not in (None, METHOD):
        device.pop("last_learning", None)
