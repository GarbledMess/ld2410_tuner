"""LD2410 Tuner - dynamic Home Assistant training and threshold optimizer."""
from __future__ import annotations

import asyncio
import base64
import re
import math
import struct
import time
import zlib
from collections import defaultdict, deque, OrderedDict
from bisect import bisect_right
from functools import lru_cache
import heapq
from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api, frontend
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.entity_registry import EVENT_ENTITY_REGISTRY_UPDATED
from homeassistant.helpers.storage import Store

from .learning import AUTO_WEIGHT, MAX_CLASS_SAMPLES, MIN_AUTO_CONFIDENCE, METHOD, fit_thresholds
from .inference import estimate_presence

DOMAIN = "ld2410_tuner"
STORAGE_VERSION = 2
STORAGE_KEY = f"{DOMAIN}.data"

GATE_RE = re.compile(r"^(?P<prefix>.+)_g(?P<gate>[0-8])_(?P<kind>move|still)_(?P<metric>energy|threshold)$")

TRAINING_STATES = {"present", "not_present", "unknown"}
MIN_SAMPLES = 20
MAX_HISTOGRAM_COUNT = 5000
HISTOGRAM_BINS = 101
AUTO_SAMPLE_INTERVAL = 2.0
AUTO_ABSENT_SCORE = 0.75
AUTO_PRESENT_CONFIRM = 3
AUTO_ABSENT_CONFIRM = 5
STORE_DELAY = 5
HISTORY_SAMPLE_INTERVAL = 5.0
HISTORY_BLOCK_SAMPLES = 60
HISTORY_RETENTION_DAYS = 30
HISTORY_RETENTION_SECONDS = HISTORY_RETENTION_DAYS * 86400
HISTORY_RETENTION_CHECK_INTERVAL = timedelta(hours=1)
HISTORY_LABELS_MAX = 1000
# Per-device feedback calibration: how much a single "incorrect" tap moves the
# effective threshold, how much a single "correct" tap relaxes it back, and
# the bounds so feedback can't push a device to always-on or always-off.
FEEDBACK_BIAS_STEP_UP = 0.4
FEEDBACK_BIAS_DECAY = 0.05
PRESENT_BIAS_MIN = -1.0
PRESENT_BIAS_MAX = 4.0
ABSENT_BIAS_MIN = 0.0
ABSENT_BIAS_MAX = AUTO_ABSENT_SCORE * 0.9
FEEDBACK_LOG_MAX = 200
HISTORY_KEYS = [f"g{gate}_{kind}" for gate in range(9) for kind in ("move", "still")]


@lru_cache(maxsize=1024)
def _decode_history_block(encoded):
    return zlib.decompress(base64.b64decode(encoded))


def _history_label_reader(device):
    """Resolve labels once, keeping last-label-wins semantics in logarithmic time."""
    intervals = list(device.get("history_labels", []))
    start = device.get("training_label_start")
    if start is not None:
        intervals.append({"start": start, "end": device.get("training_expires_at") or float("inf"),
                          "state": device.get("training_state", "unknown")})
    events = defaultdict(list)
    for order, label in enumerate(intervals):
        start, end = float(label.get("start", 0)), float(label.get("end", 0))
        if end > start:
            events[start].append((-order, end, label.get("state", "unknown")))
            events[end]  # Endpoints are needed even when no new interval starts.
    times, states, active = [], [], []
    for timestamp, additions in sorted(events.items()):
        for entry in additions:
            heapq.heappush(active, entry)
        while active and active[0][1] <= timestamp:
            heapq.heappop(active)
        times.append(timestamp)
        states.append(active[0][2] if active else None)
    def read(timestamp):
        index = bisect_right(times, timestamp) - 1
        return states[index] if index >= 0 else None
    return read


class TunerStore(Store):
    """Store subclass that tolerates older on-disk schema versions.

    The actual data-shape migration (raw "samples" arrays -> bounded
    histograms) is performed lazily, per-device, by
    TunerRuntime._migrate_device_samples() the first time each device
    is touched. Home Assistant's Store base class raises
    NotImplementedError from _async_migrate_func() by default whenever
    the stored file's version doesn't match STORAGE_VERSION, even if
    the caller never intended Store itself to do the migration. This
    override just hands the old data back unchanged so async_load()
    succeeds; TunerRuntime takes it from there.
    """

    async def _async_migrate_func(self, old_major_version, old_minor_version, old_data):
        return old_data


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    # Integration is configured exclusively through the HA UI/config flow.
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if DOMAIN in hass.data:
        return True

    store = TunerStore(hass, STORAGE_VERSION, STORAGE_KEY)
    data = await store.async_load() or {"devices": {}, "training": {}}
    runtime = TunerRuntime(hass, store, data)
    hass.data[DOMAIN] = runtime

    _register_websocket_commands(hass, runtime)
    runtime.refresh_devices(er.async_get(hass))
    runtime.subscribe_state_changes()
    runtime.unsub_sampling = async_track_time_interval(
        hass, runtime.sample_devices, timedelta(seconds=AUTO_SAMPLE_INTERVAL)
    )
    runtime.restore_timeouts()
    runtime._enforce_history_retention()
    runtime.unsub_registry = hass.bus.async_listen(
        EVENT_ENTITY_REGISTRY_UPDATED, runtime.handle_registry_update
    )
    runtime.unsub_retention = async_track_time_interval(
        hass, runtime._enforce_history_retention, HISTORY_RETENTION_CHECK_INTERVAL
    )

    static_path = Path(__file__).parent / "static"
    if not hass.data.get(f"{DOMAIN}_static_registered"):
        await hass.http.async_register_static_paths([
            StaticPathConfig("/api/ld2410_tuner/static", str(static_path), cache_headers=False)
        ])
        hass.data[f"{DOMAIN}_static_registered"] = True

    if not frontend.async_panel_exists(hass, "ld2410-tuner"):
        frontend.async_register_built_in_panel(
            hass,
            component_name="custom",
            sidebar_title="LD2410 Tuner",
            sidebar_icon="mdi:radar",
            frontend_url_path="ld2410-tuner",
            require_admin=True,
            config={
                "_panel_custom": {
                    "name": "ld2410-tuner-panel",
                    "embed_iframe": False,
                    "trust_external": False,
                    "js_url": "/api/ld2410_tuner/static/ld2410-tuner-panel.js?v=1.9.0",
                }
            },
        )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    runtime = hass.data.pop(DOMAIN, None)
    if runtime:
        if runtime.unsub:
            runtime.unsub()
        if getattr(runtime, "unsub_registry", None):
            runtime.unsub_registry()
        if getattr(runtime, "unsub_retention", None):
            runtime.unsub_retention()
        if runtime.unsub_sampling:
            runtime.unsub_sampling()
        for task in list(runtime._timeout_tasks.values()):
            task.cancel()
        runtime._timeout_tasks.clear()
        if runtime._save_task:
            runtime._save_task.cancel()
            await asyncio.gather(runtime._save_task, return_exceptions=True)
        for device_id in list(runtime._history_runtime):
            runtime._flush_history_block(device_id)
        await runtime.store.async_save(runtime.data)
    if frontend.async_panel_exists(hass, "ld2410-tuner"):
        frontend.async_remove_panel(hass, "ld2410-tuner")
    return True


class TunerRuntime:
    def __init__(self, hass: HomeAssistant, store: Store, data: dict[str, Any]) -> None:
        self.hass = hass
        self.store = store
        self.data = data
        self.unsub = None
        self.unsub_registry = None
        self.unsub_retention = None
        self.unsub_sampling = None
        self.data.setdefault("devices", {})
        self._save_task: asyncio.Task | None = None
        self._timeout_tasks: dict[str, asyncio.Task] = {}
        self._applying: set[str] = set()
        self._live: dict[str, dict[str, float]] = defaultdict(dict)
        self._auto_runtime: dict[str, dict[str, Any]] = {}
        self._history_runtime: dict[str, dict[str, Any]] = {}
        self._history_cache = OrderedDict()
        self._history_jobs = {}
        self._learning_jobs = {}

    @callback
    def subscribe_state_changes(self) -> None:
        """Subscribe only to the dynamically discovered LD2410 energy entities."""
        if self.unsub:
            self.unsub()
        registry = er.async_get(self.hass)
        entity_ids = []
        for entity in registry.entities.values():
            if entity.domain != "sensor" or not entity.device_id:
                continue
            match = GATE_RE.match(entity.entity_id.split(".", 1)[1])
            if match and match.group("metric") == "energy":
                entity_ids.append(entity.entity_id)
        self.unsub = async_track_state_change_event(
            self.hass, entity_ids, self.handle_state_change
        )

    @callback
    def handle_registry_update(self, event) -> None:
        self.refresh_devices(er.async_get(self.hass))
        self.subscribe_state_changes()

    @callback
    def refresh_devices(self, registry: er.EntityRegistry) -> None:
        before = {key: value.get("entities", {}) for key, value in self.data["devices"].items()}
        devices: dict[str, dict[str, Any]] = {}
        for entity in registry.entities.values():
            if entity.domain != "sensor" or not entity.device_id:
                continue
            match = GATE_RE.match(entity.entity_id.split(".", 1)[1])
            if not match or match.group("metric") != "energy":
                continue
            devices.setdefault(entity.device_id, {"entities": {}})
            devices[entity.device_id]["entities"][entity.entity_id] = {
                "gate": int(match.group("gate")),
                "kind": match.group("kind"),
            }

        # Keep persisted training data, but hide devices whose LD2410 entities
        # have actually been removed from the entity registry.
        for existing_id, existing in self.data["devices"].items():
            if existing_id not in devices:
                existing["entities"] = {}
        for device_id, info in devices.items():
            self.data["devices"].setdefault(device_id, {})
            self.data["devices"][device_id]["entities"] = info["entities"]
            self.data["devices"][device_id].setdefault("training_state", "unknown")

        if before != {key: value.get("entities", {}) for key, value in self.data["devices"].items()}:
            self._schedule_save()

    async def handle_state_change(self, event) -> None:
        entity_id = event.data["entity_id"]
        new_state = event.data.get("new_state")
        if new_state is None:
            return

        registry = er.async_get(self.hass)
        entity = registry.async_get(entity_id)
        if not entity or entity.domain != "sensor" or not entity.device_id:
            return

        match = GATE_RE.match(entity_id.split(".", 1)[1])
        if not match or match.group("metric") != "energy":
            return

        try:
            value = float(new_state.state)
        except (TypeError, ValueError):
            return
        if not 0 <= value <= 100:
            return

        device = self.data["devices"].get(entity.device_id)
        if not device:
            return

        key = f"g{match.group('gate')}_{match.group('kind')}"
        self._live.setdefault(entity.device_id, {})[key] = value

        # Training/history are sampled on a clock, not on value changes. Quiet
        # presence and constant empty-room values deserve equal observation time.

    @callback
    def sample_devices(self, _now=None) -> None:
        for device_id, device in self.data["devices"].items():
            values = {}
            for entity_id, info in device.get("entities", {}).items():
                state = self.hass.states.get(entity_id)
                try:
                    value = float(state.state) if state else float("nan")
                except (TypeError, ValueError):
                    continue
                if math.isfinite(value) and 0 <= value <= 100:
                    values[f"g{info['gate']}_{info['kind']}"] = value
            self._live[device_id] = values  # Drop unavailable readings, never carry them forward.
            if not values:
                self._auto_runtime.pop(device_id, None)
                device.get("auto", {}).pop("last_classification", None)
                continue
            now = time.time()
            self._migrate_device_samples(device)
            result = self._classify_auto(device, values)
            self._update_auto_state(device_id, device, result, values, now)
            self._record_history_sample(device_id, values, now)
            self._schedule_save()

    def _record_history_sample(self, device_id: str, values: dict[str, float], now: float) -> None:
        """Keep a compact time-indexed history so past periods can be labelled later."""
        runtime = self._history_runtime.setdefault(device_id, {"last_sample": 0.0, "samples": [], "start": None})
        device = self.data.get("devices", {}).get(device_id)
        if device is None:
            return
        if "history_legacy_histograms" not in device:
            device["history_legacy_histograms"] = deepcopy(device.get("histograms", {}))
        if now - float(runtime.get("last_sample", 0.0)) < HISTORY_SAMPLE_INTERVAL:
            return
        runtime["last_sample"] = now
        if runtime.get("start") is None:
            runtime["start"] = now
        row = bytes(max(0, min(100, int(round(values.get(key, 255))))) if key in values else 255 for key in HISTORY_KEYS)
        if runtime["samples"] and now - runtime["samples"][0][0] > 65535:
            self._flush_history_block(device_id)
        last = device.get("auto", {}).get("last_classification", {})
        inferred = last.get("state") if now - last.get("timestamp", 0) <= AUTO_SAMPLE_INTERVAL*2 else "unknown"
        code = {"present": 1, "not_present": 2}.get(inferred, 0)
        confidence = max(0, min(100, round(last.get("confidence", 0)*100))) if code else 0
        runtime["samples"].append((now, row + bytes((code, confidence))))
        if code:
            observations = device.setdefault("auto", {}).setdefault("observations", {})
            observations[inferred] = observations.get(inferred, 0) + 1
        label = self._history_label_at(device, now)
        if label in {"present", "not_present"}:
            for index, key in enumerate(HISTORY_KEYS):
                if row[index] > 100:
                    continue
                series = device.setdefault("histograms", {}).setdefault(key, {"present": [0] * HISTOGRAM_BINS, "not_present": [0] * HISTOGRAM_BINS})
                series[label][row[index]] += 1
                self._compress_histogram(series[label])
        if len(runtime["samples"]) >= HISTORY_BLOCK_SAMPLES:
            self._flush_history_block(device_id)

    def _flush_history_block(self, device_id: str) -> None:
        runtime = self._history_runtime.get(device_id)
        if not runtime or not runtime.get("samples"):
            return
        samples = runtime["samples"]
        start = float(samples[0][0])
        raw = bytearray()
        for timestamp, row in samples:
            offset = max(0, min(65535, int(round(timestamp - start))))
            raw.extend(struct.pack(">H", offset))
            raw.extend(row[:len(HISTORY_KEYS)])
            raw.extend(row[len(HISTORY_KEYS):len(HISTORY_KEYS)+2] if len(row) >= len(HISTORY_KEYS)+2 else bytes(2))
        block = {
            "version": 2,
            "start": start,
            "count": len(samples),
            "end": float(samples[-1][0]),
            "data": base64.b64encode(zlib.compress(bytes(raw), 6)).decode("ascii"),
        }
        device = self.data.get("devices", {}).get(device_id)
        if device is not None:
            history = device.setdefault("history", [])
            history.append(block)
            cutoff = time.time() - HISTORY_RETENTION_SECONDS
            device["history"] = [b for b in history if float(b.get("end", float(b.get("start", 0)) + 65535)) >= cutoff]
        runtime["samples"] = []
        runtime["start"] = None

    @callback
    def _enforce_history_retention(self, *_args) -> None:
        """Prune history blocks older than the retention window.

        Previously this only happened inside _flush_history_block, i.e. only
        for devices that were still actively reporting - a device that goes
        quiet or gets removed would keep its old blocks forever. This runs
        once at startup and hourly thereafter so stale history ages out
        regardless of whether the device is still live.
        """
        cutoff = time.time() - HISTORY_RETENTION_SECONDS
        changed = False
        for device in self.data.get("devices", {}).values():
            history = device.get("history")
            if not history:
                continue
            pruned = [b for b in history if float(b.get("end", float(b.get("start", 0)) + 65535)) >= cutoff]
            if len(pruned) != len(history):
                device["history"] = pruned
                changed = True
        if changed:
            self._schedule_save()

    @staticmethod
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

    async def flush_history(self) -> None:
        for device_id in list(self._history_runtime):
            self._flush_history_block(device_id)
        self._schedule_save()
        if self._save_task:
            await asyncio.shield(self._save_task)

    def _iter_history_samples(self, device: dict[str, Any], since=0, include_auto=False):
        cutoff = max(since, time.time() - HISTORY_RETENTION_SECONDS)
        for block in device.get("history", []):
            try:
                start = float(block["start"])
                if float(block.get("end", start + 65535)) < cutoff:
                    continue
                raw = _decode_history_block(block["data"])
                count = int(block["count"])
                version = block.get("version", 1)
                if version not in (1, 2):
                    continue
                stride = 2 + len(HISTORY_KEYS) + (2 if version == 2 else 0)
                if len(raw) < count * stride:
                    continue
                for index in range(count):
                    pos = index * stride
                    offset = struct.unpack(">H", raw[pos:pos + 2])[0]
                    values = raw[pos + 2:pos + stride]
                    if start + offset >= cutoff:
                        yield start + offset, values if include_auto else values[:len(HISTORY_KEYS)]
            except (KeyError, TypeError, ValueError, zlib.error, struct.error):
                continue

        for device_id, candidate in self.data["devices"].items():
            if candidate is device:
                for timestamp, row in self._history_runtime.get(device_id, {}).get("samples", []):
                    if timestamp >= cutoff:
                        yield timestamp, row if include_auto else row[:len(HISTORY_KEYS)]
                break

    def label_history_range(self, device_id: str, start: float, end: float, state: str) -> dict[str, Any]:
        """Apply a retrospective manual label to historical samples in a time range."""
        if state not in {"present", "not_present", "unknown"}:
            raise ValueError("Invalid training state")
        if not math.isfinite(start) or not math.isfinite(end) or end <= start:
            raise ValueError("End time must be after start time")
        device = self.data.get("devices", {}).get(device_id)
        if not device:
            raise ValueError("Unknown device")
        self._ensure_histograms(device)
        device.setdefault("history_legacy_histograms", deepcopy(device["histograms"]))
        # Close the active interval first; an explicit retrospective correction
        # must win over a live label when these histograms are rebuilt.
        now = time.time()
        self._close_training_interval(device, now)
        if device.get("training_state") in {"present", "not_present"}:
            device["training_label_start"] = now
        # Remove any previous retrospective labels from this exact range before
        # reapplying the requested state. This makes corrections idempotent.
        labels = device.setdefault("history_labels", [])
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
                left = dict(existing); left["end"] = start; preserved.append(left)
            if old_end > end:
                right = dict(existing); right["start"] = end; preserved.append(right)
        labels[:] = preserved
        labels.append({"start": start, "end": end, "state": state, "source": "manual"})
        # Rebuild manual histograms from all retained labelled history.
        rebuilt = {key: {"present": [0] * HISTOGRAM_BINS, "not_present": [0] * HISTOGRAM_BINS} for key in HISTORY_KEYS}
        label_at = _history_label_reader(device)
        for timestamp, row in self._iter_history_samples(device):
            label = label_at(timestamp)
            if label not in {"present", "not_present"}:
                continue
            for index, key in enumerate(HISTORY_KEYS):
                value = row[index]
                if value <= 100:
                    rebuilt[key][label][value] += 1
        legacy = device.get("history_legacy_histograms", {})
        for key, legacy_series in legacy.items():
            target = rebuilt.setdefault(key, {"present": [0] * HISTOGRAM_BINS, "not_present": [0] * HISTOGRAM_BINS})
            for label in ("present", "not_present"):
                old_hist = legacy_series.get(label, [])
                for index, count in enumerate(old_hist[:HISTOGRAM_BINS]):
                    target[label][index] += int(count)
        for series in rebuilt.values():
            self._compress_histogram(series["present"])
            self._compress_histogram(series["not_present"])
        device["histograms"] = rebuilt
        device.pop("last_learning", None)
        device["label_revision"] = device.get("label_revision", 0) + 1
        self._compact_history_labels(device)
        self._schedule_save()
        return {"ok": True, "labelled_samples": sum(1 for ts, _ in self._iter_history_samples(device) if start <= ts < end), "history_labels": labels}

    @staticmethod
    def _manual_history_state(device: dict[str, Any], timestamp: float) -> str | None:
        active_start = device.get("training_label_start")
        expires = device.get("training_expires_at")
        if active_start is not None and timestamp >= active_start and (not expires or timestamp < expires):
            return device.get("training_state", "unknown")
        for label in reversed(device.get("history_labels", [])):
            if float(label.get("start", 0)) <= timestamp < float(label.get("end", 0)):
                return label.get("state", "unknown")
        return None

    @staticmethod
    def _history_label_at(device: dict[str, Any], timestamp: float) -> str:
        return TunerRuntime._manual_history_state(device, timestamp) or "unknown"

    def history_series_multi(self, device_id: str, keys: list[str], hours: float, max_points: int = 400, end: float | None = None) -> dict[str, Any]:
        """Downsampled time series for several gates at once, sharing one pass
        over the history and one set of labels, so the chart can overlay
        every gate of a kind (move or still) together instead of one at a
        time.

        Returns min/avg/max per bucket per gate (rather than a single
        averaged value) so short spikes during a quiet period aren't
        smoothed away - those spikes are exactly what matters when judging
        whether a threshold is safely above the real noise ceiling.
        """
        device = self.data["devices"].get(device_id)
        if not device:
            raise ValueError("Unknown device")
        keys = [k for k in dict.fromkeys(keys) if k in HISTORY_KEYS]
        if not keys:
            raise ValueError("No valid gate keys given")
        indices = {k: HISTORY_KEYS.index(k) for k in keys}
        if not math.isfinite(float(hours)):
            raise ValueError("Hours must be finite")
        hours = max(0.1, min(HISTORY_RETENTION_DAYS * 24, float(hours)))
        max_points = max(50, min(1000, int(max_points)))
        if end is not None and not math.isfinite(float(end)):
            raise ValueError("End time must be finite")
        end = min(time.time(), float(end)) if end is not None else time.time()
        start = end - hours * 3600
        span = end - start

        buckets = {key: {} for key in keys}
        counts = dict.fromkeys(keys, 0)
        bucket_span = next((seconds for seconds in (6, 12, 30, 60, 120, 300, 600, 1800, 3600, 7200, 14400, 21600, 43200, 86400) if seconds >= span/max_points), 86400)
        for ts, row in self._iter_history_samples(device, start):
            if ts < start or ts > end:
                continue
            bucket = math.floor(ts / bucket_span)
            for key, index in indices.items():
                value = row[index]
                if value > 100:
                    continue
                counts[key] += 1
                acc = buckets[key].setdefault(bucket, [value, value, 0, 0, ts, ts])
                acc[0], acc[1] = min(acc[0], value), max(acc[1], value)
                acc[2] += value
                acc[3] += 1
                acc[4], acc[5] = min(acc[4], ts), max(acc[5], ts)
        series = {
            key: {"sample_count": counts[key], "points": [
                {"t": round((acc[4]+acc[5])/2, 3), "min": acc[0], "max": acc[1], "avg": round(acc[2] / acc[3], 3), "count": acc[3]}
                for index, acc in sorted(buckets[key].items())
            ]} for key in keys
        }

        labels = [
            {
                "start": max(start, float(label.get("start", 0))),
                "end": min(end, float(label.get("end", 0))),
                "state": label.get("state", "unknown"),
            }
            for label in device.get("history_labels", [])
            if float(label.get("end", 0)) >= start and float(label.get("start", 0)) <= end
        ]

        active_start = device.get("training_label_start")
        if active_start is not None:
            active_end = min(end, device.get("training_expires_at") or end)
            if active_end > max(start, active_start):
                labels.append({"start": max(start, active_start), "end": active_end, "state": device.get("training_state", "unknown")})
        return {"start": start, "end": end, "bucket_seconds": bucket_span, "series": series, "labels": labels}

    def _find_threshold_entity(self, device_id: str, key: str) -> str:
        registry = er.async_get(self.hass)
        for entity in registry.entities.values():
            if entity.domain != "number" or entity.device_id != device_id:
                continue
            match = GATE_RE.match(entity.entity_id.split(".", 1)[1])
            if match and match.group("metric") == "threshold" and f"g{match.group('gate')}_{match.group('kind')}" == key:
                return entity.entity_id
        raise ValueError("No matching threshold entity found for that gate")

    async def set_gate_threshold(self, device_id: str, key: str, value: float) -> dict[str, Any]:
        if key not in HISTORY_KEYS:
            raise ValueError("Unknown gate key")
        entity_id = self._find_threshold_entity(device_id, key)
        if not math.isfinite(float(value)) or not 0 <= float(value) <= 100:
            raise ValueError("Threshold must be between 0 and 100")
        value = round(float(value))
        await self.hass.services.async_call(
            "number", "set_value", {"entity_id": entity_id, "value": value}, blocking=True,
        )
        return {"ok": True, "entity_id": entity_id, "value": value}

    def _classify_auto(self, device: dict[str, Any], values: dict[str, float]) -> dict[str, Any]:
        auto = device.setdefault("auto", {})
        hist = auto.setdefault("all_histograms", {})
        expected = [f"g{info['gate']}_{info['kind']}" for info in device.get("entities", {}).values()]
        result = estimate_presence(values, device.get("histograms", {}), hist,
                                   auto.setdefault("filter", {}), time.time(), expected,
                                   auto.get("calibration", {}))
        # Warm up from observations, then freeze the unlabelled baseline during
        # likely occupancy so a stationary person does not become background.
        for key, value in values.items():
            h = hist.setdefault(key, [0] * HISTOGRAM_BINS)
            if sum(h) < MIN_SAMPLES or result["presence_probability"] < .7:
                h[int(round(value))] += 1
                self._compress_histogram(h)
        return result

    def _update_auto_state(self, device_id: str, device: dict[str, Any], result: dict[str, Any], values: dict[str, float], now: float) -> None:
        runtime = self._auto_runtime.setdefault(device_id, {})
        label = result["label"]
        auto = device.setdefault("auto", {})
        auto["last_classification"] = {"state": "unknown", "confidence": result["confidence"], "score": result["score"], "active_gates": result["active_gates"], "top_gates": result["top_gates"], "timestamp": now, "basis": result.get("basis"), "model": result.get("model"), "presence_probability": result.get("presence_probability")}
        previous_pending = runtime.get("pending")
        runtime["pending"] = label
        runtime["pending_count"] = int(runtime.get("pending_count", 0)) + 1 if label == previous_pending else 1
        if label == "present":
            # Do not confirm entry from residual filter memory after a spike.
            if runtime.get("state") != "present" and result["score"] <= 0:
                runtime["pending_count"] = 0
                return
            needed = AUTO_PRESENT_CONFIRM
        elif label == "not_present":
            needed = AUTO_ABSENT_CONFIRM
        else:
            runtime["pending_count"] = 0
            runtime["state"] = "unknown"
            segments = auto.get("segments", [])
            if segments and segments[-1].get("end") is None:
                segments[-1]["end"] = now
            return
        if runtime["pending_count"] < needed:
            return
        old = runtime.get("state", "unknown")
        runtime["state"] = label
        auto = device.setdefault("auto", {})
        auto["last_classification"] = {"state": label, "confidence": result["confidence"], "score": result["score"], "active_gates": result["active_gates"], "top_gates": result["top_gates"], "timestamp": now, "basis": result.get("basis"), "model": result.get("model"), "presence_probability": result.get("presence_probability")}
        segments = auto.setdefault("segments", [])
        if old != label:
            if segments and segments[-1].get("end") is None:
                segments[-1]["end"] = now
            segments.append({"start": now, "end": None, "state": label, "confidence": result["confidence"], "score": result["score"]})
        if len(segments) > 500:
            del segments[:-500]

    def auto_learning_summary(self, device: dict[str, Any]) -> dict[str, Any]:
        auto = device.get("auto", {})
        observation_counts = sum(auto.get("observations", {}).values())
        segments = auto.get("segments", [])
        last = auto.get("last_classification")
        if last and time.time() - last.get("timestamp", 0) > 15:
            last = None
        counts = defaultdict(int)
        for seg in segments:
            counts[seg.get("state", "unknown")] += 1
        feedback_log = auto.get("feedback", [])
        feedback_summary = {}
        for label in ("present", "not_present"):
            entries = [f for f in feedback_log if f.get("label") == label]
            if entries:
                feedback_summary[label] = {
                    "correct": sum(1 for f in entries if f.get("correct")),
                    "total": len(entries),
                }
        return {
            "enabled": True,
            "observations": observation_counts,
            "observations_by_state": dict(auto.get("observations", {})),
            "segments": len(segments),
            "segments_by_state": dict(counts),
            "last": last,
            "role": "confidence_weighted_training",
            "weight": AUTO_WEIGHT,
            "feedback": feedback_summary,
            "calibration": dict(auto.get("calibration", {"present_bias": 0.0, "absent_bias": 0.0})),
            "note": "Automatic estimates contribute at 20% × confidence. Human labels always take priority; inferred proportions do not block Apply.",
        }

    def record_auto_feedback(self, device_id: str, correct: bool) -> dict[str, Any]:
        """Record a Correct/Incorrect tap on the last auto classification.

        Nudges a small per-device calibration bias: an "Incorrect" tap raises
        the bar for whichever label was actually shown (present or not
        present) so that specific device needs stronger evidence next time;
        a "Correct" tap slowly relaxes that bias back down. This is
        intentionally asymmetric - fast to react to a mistake, slow to
        relax - so a couple of wrong reads can't be undone by one lucky
        confirmation, but sustained correct feedback still recovers
        sensitivity over time.
        """
        device = self.data["devices"].get(device_id)
        if not device:
            raise ValueError("Unknown device")
        auto = device.setdefault("auto", {})
        last = auto.get("last_classification")
        if not last or time.time() - last.get("timestamp", 0) > 15 or last.get("state") not in ("present", "not_present"):
            raise ValueError("No confident automatic classification to give feedback on yet")
        label = last["state"]
        calibration = auto.setdefault("calibration", {"present_bias": 0.0, "absent_bias": 0.0})
        if label == "present":
            bias = float(calibration.get("present_bias", 0.0))
            bias = bias + FEEDBACK_BIAS_STEP_UP if not correct else bias - FEEDBACK_BIAS_DECAY
            calibration["present_bias"] = max(PRESENT_BIAS_MIN, min(PRESENT_BIAS_MAX, bias))
        else:
            bias = float(calibration.get("absent_bias", 0.0))
            bias = bias + FEEDBACK_BIAS_STEP_UP if not correct else bias - FEEDBACK_BIAS_DECAY
            calibration["absent_bias"] = max(ABSENT_BIAS_MIN, min(ABSENT_BIAS_MAX, bias))

        feedback_log = auto.setdefault("feedback", [])
        feedback_log.append({
            "timestamp": time.time(),
            "label": label,
            "correct": bool(correct),
            "score": last.get("score"),
            "confidence": last.get("confidence"),
        })
        if len(feedback_log) > FEEDBACK_LOG_MAX:
            del feedback_log[:-FEEDBACK_LOG_MAX]
        self._schedule_save()
        return {"ok": True, "label": label, "calibration": dict(calibration)}

    def _migrate_device_samples(self, device: dict[str, Any]) -> None:
        """Convert the old raw sample arrays to bounded histograms once."""
        if "histograms" in device:
            return
        histograms: dict[str, dict[str, list[int]]] = {}
        for key, series in device.pop("samples", {}).items():
            out = {"present": [0] * HISTOGRAM_BINS, "not_present": [0] * HISTOGRAM_BINS}
            for state in ("present", "not_present"):
                for value in series.get(state, []):
                    try:
                        value = max(0, min(100, int(round(float(value)))))
                    except (TypeError, ValueError):
                        continue
                    out[state][value] += 1
                self._compress_histogram(out[state])
            histograms[key] = out
        device["histograms"] = histograms

    @staticmethod
    def _compress_histogram(histogram: list[int]) -> None:
        total = sum(histogram)
        if total <= MAX_HISTOGRAM_COUNT:
            return
        # Keep a bounded, recency-weighted distribution. Halving makes recent
        # observations progressively more important without storing raw samples.
        for i, value in enumerate(histogram):
            histogram[i] = value // 2

    def _ensure_histograms(self, device: dict[str, Any]) -> dict[str, dict[str, list[int]]]:
        self._migrate_device_samples(device)
        return device.setdefault("histograms", {})

    def restore_timeouts(self) -> None:
        for device_id, device in self.data.get("devices", {}).items():
            expires_at = device.get("training_expires_at")
            if expires_at and device.get("training_state") in {"present", "not_present"}:
                self._schedule_timeout(device_id, max(0, float(expires_at) - time.time()))

    def set_training_state(self, device_id: str, state: str, timeout_seconds: int | None = None) -> None:
        if state not in TRAINING_STATES:
            raise ValueError("Invalid training state")
        device = self.data["devices"].get(device_id)
        if not device:
            raise ValueError("Unknown device")
        old_task = self._timeout_tasks.pop(device_id, None)
        if old_task:
            old_task.cancel()

        now = time.time()
        self._close_training_interval(device, now)
        device["training_state"] = state
        device["label_revision"] = device.get("label_revision", 0) + 1
        if state in {"present", "not_present"}:
            device["training_label_start"] = now
        if state in {"present", "not_present"} and timeout_seconds:
            timeout_seconds = max(1, int(timeout_seconds))
            device["training_timeout_seconds"] = timeout_seconds
            device["training_expires_at"] = time.time() + timeout_seconds
            self._schedule_timeout(device_id, timeout_seconds)
        else:
            device.pop("training_timeout_seconds", None)
            device.pop("training_expires_at", None)
        self._schedule_save()

    def _close_training_interval(self, device, now):
        active_start = device.pop("training_label_start", None)
        state = device.get("training_state")
        end = min(now, device.get("training_expires_at") or now)
        if state in {"present", "not_present"} and active_start is not None and end > active_start:
            device.setdefault("history_labels", []).append({"start": active_start, "end": end, "state": state, "source": "live"})
            self._compact_history_labels(device)

    def _schedule_timeout(self, device_id: str, seconds: float) -> None:
        old_task = self._timeout_tasks.pop(device_id, None)
        if old_task:
            old_task.cancel()
        self._timeout_tasks[device_id] = self.hass.async_create_task(
            self._timeout_worker(device_id, seconds)
        )

    async def _timeout_worker(self, device_id: str, seconds: float) -> None:
        try:
            await asyncio.sleep(seconds)
            device = self.data.get("devices", {}).get(device_id)
            if device and device.get("training_expires_at") and device["training_expires_at"] <= time.time():
                self._close_training_interval(device, time.time())
                device["training_state"] = "unknown"
                device.pop("training_label_start", None)
                device.pop("training_expires_at", None)
                self._schedule_save()
        except asyncio.CancelledError:
            return
        finally:
            if self._timeout_tasks.get(device_id) is asyncio.current_task():
                self._timeout_tasks.pop(device_id, None)

    def clear_samples(self, device_id: str) -> None:
        if device_id in self.data["devices"]:
            self.set_training_state(device_id, "unknown")
            self.data["devices"][device_id].pop("last_learning", None)
            self.data["devices"][device_id]["label_revision"] = self.data["devices"][device_id].get("label_revision", 0) + 1
            self.data["devices"][device_id]["histograms"] = {}
            self.data["devices"][device_id].pop("auto", None)
            self.data["devices"][device_id].pop("history", None)
            self.data["devices"][device_id].pop("history_labels", None)
            self.data["devices"][device_id].pop("history_legacy_histograms", None)
            self._live.pop(device_id, None)
            self._auto_runtime.pop(device_id, None)
            self._history_runtime.pop(device_id, None)
            self._schedule_save()

    def _history_view(self, device_id):
        device = self.data["devices"].get(device_id)
        if not device:
            raise ValueError("Unknown device")
        snapshot = {key: device.get(key) for key in ("training_state", "training_label_start", "training_expires_at")}
        snapshot["history"] = list(device.get("history", []))
        snapshot["history_labels"] = deepcopy(device.get("history_labels", []))
        view = TunerRuntime(None, None, {"devices": {device_id: snapshot}})
        view._history_runtime[device_id] = {"samples": list(self._history_runtime.get(device_id, {}).get("samples", []))}
        return view

    async def async_history_series(self, device_id, keys, hours, max_points, end=None):
        if end is not None and not math.isfinite(float(end)):
            raise ValueError("End time must be finite")
        end = min(time.time(), float(end)) if end is not None else time.time()
        device = self.data["devices"].get(device_id)
        if not device:
            raise ValueError("Unknown device")
        cache_key = (device_id, tuple(sorted(set(keys))), hours, max_points, end,
                     device.get("label_revision", 0), device.get("training_state"),
                     device.get("training_label_start"), device.get("training_expires_at"))
        if cache_key in self._history_cache:
            self._history_cache.move_to_end(cache_key)
            return self._history_cache[cache_key]
        if cache_key not in self._history_jobs:
            view = self._history_view(device_id)
            async def build():
                try:
                    result = await self.hass.async_add_executor_job(view.history_series_multi, device_id, keys, hours, max_points, end)
                    self._history_cache[cache_key] = result
                    while len(self._history_cache) > 16:
                        self._history_cache.popitem(last=False)
                    return result
                finally:
                    self._history_jobs.pop(cache_key, None)
            self._history_jobs[cache_key] = asyncio.create_task(build())
        return await asyncio.shield(self._history_jobs[cache_key])

    async def async_learn(self, device_id):
        if device_id not in self._learning_jobs:
            self._learning_jobs[device_id] = asyncio.create_task(self._learn_once(device_id))
        return await asyncio.shield(self._learning_jobs[device_id])

    async def _learn_once(self, device_id):
        try:
            entities, current = self._threshold_configuration(device_id)
            view = self._history_view(device_id)
            device = self.data["devices"][device_id]
            revision = device.get("label_revision", 0)
            learned = await self.hass.async_add_executor_job(view._fit_history, device_id, entities, current)
            if revision != device.get("label_revision", 0):
                raise ValueError("Training labels changed while learning; learn again")
            learned["label_revision"] = revision
            device["last_learning"] = learned
            self._schedule_save()
            return learned
        finally:
            self._learning_jobs.pop(device_id, None)

    def _fit_history(self, device_id, entities, current):
        device = self.data["devices"][device_id]
        groups = {label: deque(maxlen=MAX_CLASS_SAMPLES) for label in ("present", "not_present")}
        automatic = {label: deque(maxlen=MAX_CLASS_SAMPLES) for label in groups}
        indices = [HISTORY_KEYS.index(key) for key in entities]
        label_at = _history_label_reader(device)
        for ts, row in self._iter_history_samples(device, include_auto=True):
            if not any(row[index] <= 100 for index in indices):
                continue
            label = label_at(ts)
            if label in groups:
                groups[label].append((ts, row, label))
            elif label is None and len(row) >= len(HISTORY_KEYS)+2:
                inferred = {1: "present", 2: "not_present"}.get(row[len(HISTORY_KEYS)])
                confidence = row[len(HISTORY_KEYS)+1] / 100
                if inferred and confidence >= MIN_AUTO_CONFIDENCE:
                    automatic[inferred].append((ts, row, inferred, confidence))
        values = lambda row: {key: row[i] for i, key in enumerate(HISTORY_KEYS) if row[i] <= 100}
        rows = [(ts, values(row), label) for group in groups.values() for ts, row, label in group]
        guesses = [(ts, values(row), label, confidence) for group in automatic.values() for ts, row, label, confidence in group]
        learned = fit_thresholds(rows, list(entities), current, guesses)
        learned["configuration"] = current
        learned["entities"] = entities
        learned["created_at"] = time.time()
        return learned

    def _threshold_configuration(self, device_id):
        entities, current, limits = {}, {}, {}
        registry = er.async_get(self.hass)
        for entity in registry.entities.values():
            if entity.device_id != device_id or entity.domain != "number":
                continue
            state = self.hass.states.get(entity.entity_id)
            try:
                value = float(state.state) if state else float("nan")
            except (TypeError, ValueError):
                value = float("nan")
            for kind in ("move", "still"):
                if entity.entity_id.endswith(f"max_{kind}_distance_gate"):
                    if not math.isfinite(value) or not 2 <= value <= 8 or value != int(value):
                        raise ValueError("Maximum distance gate is unavailable or outside 2–8")
                    limits[kind] = int(value)
            match = GATE_RE.match(entity.entity_id.split(".", 1)[1])
            if match and match.group("metric") == "threshold":
                key = f"g{match.group('gate')}_{match.group('kind')}"
                entities[key] = entity.entity_id
                if math.isfinite(value):
                    current[key] = value
        entities = {key: entity_id for key, entity_id in entities.items() if int(key[1]) <= limits.get(key.split("_")[1], 8)}
        expected = {f"g{gate}_{kind}" for kind in ("move", "still") for gate in range(limits.get(kind, 8) + 1)}
        missing = expected - set(entities)
        if missing:
            raise ValueError("Enable all active gate threshold entities before learning: " + ", ".join(sorted(missing)))
        current = {key: value for key, value in current.items() if key in entities}
        return entities, current

    async def apply(self, device_id: str) -> dict[str, Any]:
        if device_id in self._applying:
            raise ValueError("Threshold application is already in progress")
        self._applying.add(device_id)
        try:
            return await self._apply_validated(device_id)
        finally:
            self._applying.discard(device_id)

    async def _apply_validated(self, device_id: str) -> dict[str, Any]:
        device = self.data["devices"].get(device_id)
        if not device:
            raise ValueError("Unknown device")
        learned = device.get("last_learning") or {}
        if learned.get("method") != METHOD or learned.get("status") != "ok":
            raise ValueError("Learn thresholds with the current model before applying")
        if learned.get("label_revision", device.get("label_revision", 0)) != device.get("label_revision", 0):
            raise ValueError("Training labels changed; learn again before applying")
        entities, current = self._threshold_configuration(device_id)
        if entities != learned.get("entities") or current != learned.get("configuration") or set(current) != set(entities):
            raise ValueError("Threshold configuration changed or is unavailable; learn again before applying")
        # A device receives serial configuration commands. Raise noisy thresholds
        # before lowering sensitive ones and stop on the first partial failure.
        changes = sorted(entities, key=lambda key: learned["proposals"][key]["threshold"] - current[key], reverse=True)
        applied, skipped = {}, {}
        failed = False
        for key in changes:
            value = learned["proposals"][key]["threshold"]
            if failed:
                skipped[key] = "Not attempted after an earlier write failed"
                continue
            try:
                await self.set_gate_threshold(device_id, key, value)
                applied[key] = value
            except Exception as err:
                skipped[key] = str(err)
                failed = True
        device["last_applied"] = applied
        self._schedule_save()
        return {"applied": applied, "skipped": skipped}

    def _schedule_save(self) -> None:
        if self._save_task and not self._save_task.done():
            return
        self._save_task = self.hass.async_create_task(self._delayed_save())

    async def _delayed_save(self) -> None:
        await asyncio.sleep(STORE_DELAY)
        await self.store.async_save(self.data)

    def export_data(self, device_id: str | None = None) -> dict[str, Any]:
        registry = er.async_get(self.hass)
        self.refresh_devices(registry)
        devices = [device_id] if device_id else list(self.data.get("devices", {}))
        exported = {"format": "ld2410_tuner_export_v2", "generated_at": time.time(), "devices": {}}
        devreg = dr.async_get(self.hass)
        for did in devices:
            device = self.data.get("devices", {}).get(did)
            if not device:
                continue
            dev = devreg.async_get(did)
            histograms = self._ensure_histograms(device)
            current_thresholds = {}
            for entity in registry.entities.values():
                if entity.device_id != did or entity.domain != "number":
                    continue
                match = GATE_RE.match(entity.entity_id.split(".", 1)[1])
                if not match or match.group("metric") != "threshold":
                    continue
                state = self.hass.states.get(entity.entity_id)
                if state is None or state.state in ("unknown", "unavailable"):
                    continue
                try:
                    current_thresholds[f"g{match.group('gate')}_{match.group('kind')}"] = float(state.state)
                except ValueError:
                    continue

            exported["devices"][did] = {
                "name": (dev.name_by_user or dev.name or did) if dev else did,
                "area_id": dev.area_id if dev else None,
                "training_state": device.get("training_state", "unknown"),
                "sample_counts": {k: {"present": sum(v.get("present", [])), "not_present": sum(v.get("not_present", []))} for k, v in histograms.items()},
                "histograms": histograms,
                "histogram_stats": {k: {"present": histogram_summary(v.get("present", [])), "not_present": histogram_summary(v.get("not_present", []))} for k, v in histograms.items()},
                "current_thresholds": current_thresholds,
                "last_learning": device.get("last_learning"),
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
        return exported

    def snapshot(self) -> dict[str, Any]:
        registry = er.async_get(self.hass)
        self.refresh_devices(registry)
        result = {"devices": {}}

        for device_id, device in self.data["devices"].items():
            entity_ids = device.get("entities", {})
            if not entity_ids:
                continue

            # Device name/area are resolved from the registry.
            name = device_id
            area_id = None
            devreg = None
            from homeassistant.helpers import device_registry as dr
            devreg = dr.async_get(self.hass)
            dev = devreg.async_get(device_id)
            if dev:
                name = dev.name_by_user or dev.name or name
                area_id = dev.area_id

            thresholds = {}
            current = {}
            for entity in registry.entities.values():
                if entity.device_id != device_id or entity.domain != "number":
                    continue
                match = GATE_RE.match(entity.entity_id.split(".", 1)[1])
                if match and match.group("metric") == "threshold":
                    key = f"g{match.group('gate')}_{match.group('kind')}"
                    state = self.hass.states.get(entity.entity_id)
                    if state and state.state not in ("unknown", "unavailable"):
                        try:
                            current[key] = float(state.state)
                        except ValueError:
                            pass

            histograms = self._ensure_histograms(device)
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

            result["devices"][device_id] = {
                "name": name,
                "area_id": area_id,
                "training_state": device.get("training_state", "unknown"),
                "training_expires_at": device.get("training_expires_at"),
                "training_timeout_seconds": device.get("training_timeout_seconds", 0),
                "training_label_start": device.get("training_label_start"),
                "sample_counts": sample_counts,
                "histogram_stats": histogram_stats,
                "compression": {"type": "bounded_histogram", "max_effective_samples_per_series": MAX_HISTOGRAM_COUNT, "bins": HISTOGRAM_BINS},
                "current_thresholds": current,
                "last_learning": device.get("last_learning"),
                "last_applied": device.get("last_applied", {}),
                "auto_learning": self.auto_learning_summary(device),
                "history": {
                    "retention_days": HISTORY_RETENTION_DAYS,
                    "revision": device.get("label_revision", 0),
                    "blocks": len(device.get("history", [])),
                    "samples": sum(int(b.get("count", 0)) for b in device.get("history", [])),
                    "labels": device.get("history_labels", []),
                    "oldest": min((float(b.get("start", 0)) for b in device.get("history", [])), default=None),
                    "newest": max((float(b.get("start", 0)) for b in device.get("history", [])), default=None),
                },
            }
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

def _register_websocket_commands(hass: HomeAssistant, runtime: TunerRuntime) -> None:
    @websocket_api.require_admin
    @websocket_api.websocket_command(
        {vol.Required("type"): f"{DOMAIN}/snapshot"}
    )
    @websocket_api.async_response
    async def snapshot(hass, connection, msg):
        runtime = hass.data.get(DOMAIN)
        if runtime is None:
            connection.send_error(msg["id"], "not_loaded", "LD2410 Tuner is not loaded")
            return
        connection.send_result(msg["id"], runtime.snapshot())

    @websocket_api.require_admin
    @websocket_api.websocket_command(
        {
            vol.Required("type"): f"{DOMAIN}/set_training_state",
            vol.Required("device_id"): str,
            vol.Required("state"): vol.In(TRAINING_STATES),
            vol.Optional("timeout_seconds", default=0): vol.All(vol.Coerce(int), vol.Range(min=0)),
        }
    )
    @websocket_api.async_response
    async def set_state(hass, connection, msg):
        runtime = hass.data.get(DOMAIN)
        if runtime is None:
            connection.send_error(msg["id"], "not_loaded", "LD2410 Tuner is not loaded")
            return
        timeout = msg.get("timeout_seconds") or None
        try:
            runtime.set_training_state(msg["device_id"], msg["state"], timeout)
        except ValueError as err:
            connection.send_error(msg["id"], "invalid_device", str(err))
            return
        connection.send_result(msg["id"], {"ok": True})

    @websocket_api.require_admin
    @websocket_api.websocket_command(
        {
            vol.Required("type"): f"{DOMAIN}/learn",
            vol.Required("device_id"): str,
        }
    )
    @websocket_api.async_response
    async def learn(hass, connection, msg):
        runtime = hass.data.get(DOMAIN)
        if runtime is None:
            connection.send_error(msg["id"], "not_loaded", "LD2410 Tuner is not loaded")
            return
        try:
            result = await runtime.async_learn(msg["device_id"])
        except ValueError as err:
            connection.send_error(msg["id"], "invalid_device", str(err))
            return
        connection.send_result(msg["id"], result)

    @websocket_api.require_admin
    @websocket_api.websocket_command(
        {
            vol.Required("type"): f"{DOMAIN}/apply",
            vol.Required("device_id"): str,
        }
    )
    @websocket_api.async_response
    async def apply(hass, connection, msg):
        runtime = hass.data.get(DOMAIN)
        if runtime is None:
            connection.send_error(msg["id"], "not_loaded", "LD2410 Tuner is not loaded")
            return
        try:
            result = await runtime.apply(msg["device_id"])
        except ValueError as err:
            connection.send_error(msg["id"], "invalid_device", str(err))
            return
        connection.send_result(msg["id"], result)

    @websocket_api.require_admin
    @websocket_api.websocket_command(
        {
            vol.Required("type"): f"{DOMAIN}/export",
            vol.Optional("device_id"): str,
        }
    )
    @websocket_api.async_response
    async def export(hass, connection, msg):
        runtime = hass.data.get(DOMAIN)
        if runtime is None:
            connection.send_error(msg["id"], "not_loaded", "LD2410 Tuner is not loaded")
            return
        connection.send_result(msg["id"], runtime.export_data(msg.get("device_id")))

    @websocket_api.require_admin
    @websocket_api.websocket_command(
        {
            vol.Required("type"): f"{DOMAIN}/label_history",
            vol.Required("device_id"): str,
            vol.Required("start"): vol.Coerce(float),
            vol.Required("end"): vol.Coerce(float),
            vol.Required("state"): vol.In({"present", "not_present", "unknown"}),
        }
    )
    @websocket_api.async_response
    async def label_history(hass, connection, msg):
        runtime = hass.data.get(DOMAIN)
        if runtime is None:
            connection.send_error(msg["id"], "not_loaded", "LD2410 Tuner is not loaded")
            return
        try:
            result = runtime.label_history_range(msg["device_id"], msg["start"], msg["end"], msg["state"])
        except ValueError as err:
            connection.send_error(msg["id"], "invalid_history_range", str(err))
            return
        connection.send_result(msg["id"], result)

    @websocket_api.require_admin
    @websocket_api.websocket_command(
        {
            vol.Required("type"): f"{DOMAIN}/clear",
            vol.Required("device_id"): str,
        }
    )
    @websocket_api.async_response
    async def clear(hass, connection, msg):
        runtime = hass.data.get(DOMAIN)
        if runtime is None:
            connection.send_error(msg["id"], "not_loaded", "LD2410 Tuner is not loaded")
            return
        runtime.clear_samples(msg["device_id"])
        connection.send_result(msg["id"], {"ok": True})

    @websocket_api.require_admin
    @websocket_api.websocket_command(
        {
            vol.Required("type"): f"{DOMAIN}/auto_feedback",
            vol.Required("device_id"): str,
            vol.Required("correct"): bool,
        }
    )
    @websocket_api.async_response
    async def auto_feedback(hass, connection, msg):
        runtime = hass.data.get(DOMAIN)
        if runtime is None:
            connection.send_error(msg["id"], "not_loaded", "LD2410 Tuner is not loaded")
            return
        try:
            result = runtime.record_auto_feedback(msg["device_id"], msg["correct"])
        except ValueError as err:
            connection.send_error(msg["id"], "invalid_device", str(err))
            return
        connection.send_result(msg["id"], result)

    @websocket_api.require_admin
    @websocket_api.websocket_command(
        {
            vol.Required("type"): f"{DOMAIN}/history_series_multi",
            vol.Required("device_id"): str,
            vol.Required("keys"): [str],
            vol.Optional("hours", default=6): vol.Coerce(float),
            vol.Optional("max_points", default=400): vol.Coerce(int),
            vol.Optional("end"): vol.Coerce(float),
        }
    )
    @websocket_api.async_response
    async def history_series_multi(hass, connection, msg):
        runtime = hass.data.get(DOMAIN)
        if runtime is None:
            connection.send_error(msg["id"], "not_loaded", "LD2410 Tuner is not loaded")
            return
        try:
            result = await runtime.async_history_series(msg["device_id"], msg["keys"], msg["hours"], msg["max_points"], msg.get("end"))
        except ValueError as err:
            connection.send_error(msg["id"], "invalid_request", str(err))
            return
        connection.send_result(msg["id"], result)

    @websocket_api.require_admin
    @websocket_api.websocket_command(
        {
            vol.Required("type"): f"{DOMAIN}/set_gate_threshold",
            vol.Required("device_id"): str,
            vol.Required("key"): str,
            vol.Required("value"): vol.Coerce(float),
        }
    )
    @websocket_api.async_response
    async def set_gate_threshold(hass, connection, msg):
        runtime = hass.data.get(DOMAIN)
        if runtime is None:
            connection.send_error(msg["id"], "not_loaded", "LD2410 Tuner is not loaded")
            return
        try:
            result = await runtime.set_gate_threshold(msg["device_id"], msg["key"], msg["value"])
        except ValueError as err:
            connection.send_error(msg["id"], "invalid_request", str(err))
            return
        connection.send_result(msg["id"], result)

    websocket_api.async_register_command(hass, snapshot)
    websocket_api.async_register_command(hass, set_state)
    websocket_api.async_register_command(hass, learn)
    websocket_api.async_register_command(hass, apply)
    websocket_api.async_register_command(hass, clear)
    websocket_api.async_register_command(hass, label_history)
    websocket_api.async_register_command(hass, export)
    websocket_api.async_register_command(hass, auto_feedback)
    websocket_api.async_register_command(hass, history_series_multi)
    websocket_api.async_register_command(hass, set_gate_threshold)
