"""Presentation operations on the shared runtime state."""

from __future__ import annotations

import time
from typing import Any

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from ..calibration.results import saved_results
from ..const import GATE_RE, HISTOGRAM_BINS, HISTORY_RETENTION_DAYS, MAX_HISTOGRAM_COUNT
from ..runtime.schedule import settings


def export_data(runtime, device_id: str | None = None) -> dict[str, Any]:
    registry = er.async_get(runtime.hass)
    runtime.refresh_devices(registry)
    devices = [device_id] if device_id else list(runtime.data.get("devices", {}))
    exported = {"format": "ld2410_tuner_export_v2", "generated_at": time.time(), "devices": {}}
    devreg = dr.async_get(runtime.hass)
    _export_devices(runtime, devices, devreg, registry, exported)
    return exported


def snapshot(runtime) -> dict[str, Any]:
    registry = er.async_get(runtime.hass)
    runtime.refresh_devices(registry)
    result = {"devices": {}, "learning_schedule": settings(runtime)}

    _snapshot_devices(runtime, registry, result)
    return result


def histogram_summary(histogram: list[int]) -> dict[str, Any]:
    count = sum(histogram)
    if not count:
        return {"count": 0, "min": None, "max": None, "p50": None, "p95": None, "p99": None}
    values = [i for i, n in enumerate(histogram) if n]
    return {
        "count": count,
        "min": values[0],
        "max": values[-1],
        "p50": histogram_quantile(histogram, 0.50),
        "p95": histogram_quantile(histogram, 0.95),
        "p99": histogram_quantile(histogram, 0.99),
    }


def histogram_quantile(histogram: list[int], quantile: float) -> int:
    total = sum(histogram)
    if not total:
        return 0
    target = max(1, int((total - 1) * quantile) + 1)
    seen = 0
    for value, count in enumerate(histogram):
        seen += count
        if seen >= target:
            return value
    return len(histogram) - 1


def _snapshot_devices(runtime, registry, result):
    for device_id, device in runtime.data["devices"].items():
        view = _snapshot_device(runtime, device_id, device, registry)
        if view is not None:
            result["devices"][device_id] = view


def _export_devices(runtime, devices, devreg, registry, exported):
    for did in devices:
        view = _export_device(runtime, did, devreg, registry)
        if view is not None:
            exported["devices"][did] = view


def _snapshot_device(runtime, device_id, device, registry):
    entity_ids = device.get("entities", {})
    if not entity_ids:
        return None

    # Device name/area are resolved from the registry.
    name, area_id = _snapshot_identity(runtime, device_id)

    current = _snapshot_thresholds(runtime, device_id, registry)

    histograms = runtime._ensure_histograms(device)
    sample_counts, histogram_stats = _histogram_details(histograms)

    return {
        "name": name,
        "area_id": area_id,
        "training_state": device.get("training_state", "unknown"),
        "training_expires_at": device.get("training_expires_at"),
        "training_timeout_seconds": device.get("training_timeout_seconds", 0),
        "training_label_start": device.get("training_label_start"),
        "sample_counts": sample_counts,
        "histogram_stats": histogram_stats,
        "compression": {
            "type": "bounded_histogram",
            "max_effective_samples_per_series": MAX_HISTOGRAM_COUNT,
            "bins": HISTOGRAM_BINS,
        },
        "current_thresholds": current,
        "last_learning": device.get("last_learning"),
        "learning_results": saved_results(device),
        "nightly_learning": device.get("nightly_learning"),
        "last_applied": device.get("last_applied", {}),
        "auto_learning": runtime.auto_learning_summary(device),
        "history": {
            "retention_days": HISTORY_RETENTION_DAYS,
            "revision": device.get("label_revision", 0),
            "blocks": len(device.get("history", [])),
            "samples": sum(int(b.get("count", 0)) for b in device.get("history", [])),
            "labels": device.get("history_labels", []),
            "oldest": min(
                (float(b.get("start", 0)) for b in device.get("history", [])), default=None
            ),
            "newest": max(
                (float(b.get("start", 0)) for b in device.get("history", [])), default=None
            ),
        },
    }


def _export_device(runtime, did, devreg, registry):
    device = runtime.data.get("devices", {}).get(did)
    if not device:
        return None
    dev = devreg.async_get(did)
    histograms = runtime._ensure_histograms(device)
    current_thresholds = _snapshot_thresholds(runtime, did, registry)

    return {
        "name": (dev.name_by_user or dev.name or did) if dev else did,
        "area_id": dev.area_id if dev else None,
        "training_state": device.get("training_state", "unknown"),
        "sample_counts": {
            k: {"present": sum(v.get("present", [])), "not_present": sum(v.get("not_present", []))}
            for k, v in histograms.items()
        },
        "histograms": histograms,
        "histogram_stats": {
            k: {
                "present": histogram_summary(v.get("present", [])),
                "not_present": histogram_summary(v.get("not_present", [])),
            }
            for k, v in histograms.items()
        },
        "current_thresholds": current_thresholds,
        "last_learning": device.get("last_learning"),
        "learning_results": saved_results(device),
        "nightly_learning": device.get("nightly_learning"),
        "last_applied": device.get("last_applied", {}),
        "auto_learning": device.get("auto", {}),
        "history": {
            "retention_days": HISTORY_RETENTION_DAYS,
            "revision": device.get("label_revision", 0),
            "labels": device.get("history_labels", []),
            "blocks": len(device.get("history", [])),
            "samples": sum(int(b.get("count", 0)) for b in device.get("history", [])),
        },
    }


def _snapshot_identity(runtime, device_id):
    name = device_id
    area_id = None
    devreg = dr.async_get(runtime.hass)
    dev = devreg.async_get(device_id)
    if dev:
        name = dev.name_by_user or dev.name or name
        area_id = dev.area_id
    return name, area_id


def _snapshot_thresholds(runtime, device_id, registry):
    current = {}
    for entity in registry.entities.values():
        if entity.device_id != device_id or entity.domain != "number":
            continue
        match = GATE_RE.match(entity.entity_id.split(".", 1)[1])
        if match and match.group("metric") == "threshold":
            key = f"g{match.group('gate')}_{match.group('kind')}"
            value = _threshold_reading(runtime.hass.states.get(entity.entity_id))
            if value is not None:
                current[key] = value
    return current


def _histogram_details(histograms):
    sample_counts = {}
    histogram_stats = {}
    for key, series in histograms.items():
        present = series.get("present", [0] * HISTOGRAM_BINS)
        absent = series.get("not_present", [0] * HISTOGRAM_BINS)
        sample_counts[key] = {
            "present": sum(present),
            "not_present": sum(absent),
        }
        histogram_stats[key] = {
            "present": histogram_summary(present),
            "not_present": histogram_summary(absent),
        }
    return sample_counts, histogram_stats


def _threshold_reading(state):
    if state is None or state.state in ("unknown", "unavailable"):
        return None
    try:
        return float(state.state)
    except ValueError:
        return None
