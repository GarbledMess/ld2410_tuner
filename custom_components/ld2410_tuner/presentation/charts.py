"""Charts operations on the shared runtime state."""

from __future__ import annotations

import asyncio
import math
import time
from typing import Any

from ..const import HISTORY_KEYS, HISTORY_RETENTION_DAYS


def history_series_multi(
    runtime,
    device_id: str,
    keys: list[str],
    hours: float,
    max_points: int = 400,
    end: float | None = None,
) -> dict[str, Any]:
    """Downsampled time series for several gates at once, sharing one pass
    over the history and one set of labels, so the chart can overlay
    every gate of a kind (move or still) together instead of one at a
    time.

    Returns min/avg/max per bucket per gate (rather than a single
    averaged value) so short spikes during a quiet period aren't
    smoothed away - those spikes are exactly what matters when judging
    whether a threshold is safely above the real noise ceiling.
    """
    device = runtime.data["devices"].get(device_id)
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
    bucket_span = next(
        (
            seconds
            for seconds in (
                6,
                12,
                30,
                60,
                120,
                300,
                600,
                1800,
                3600,
                7200,
                14400,
                21600,
                43200,
                86400,
            )
            if seconds >= span / max_points
        ),
        86400,
    )
    _aggregate_buckets(runtime, device, start, end, bucket_span, indices, counts, buckets)
    series = {
        key: {
            "sample_count": counts[key],
            "points": [
                {
                    "t": round((acc[4] + acc[5]) / 2, 3),
                    "min": acc[0],
                    "max": acc[1],
                    "avg": round(acc[2] / acc[3], 3),
                    "count": acc[3],
                }
                for index, acc in sorted(buckets[key].items())
            ],
        }
        for key in keys
    }

    labels = _chart_labels(device, start, end)
    return {
        "start": start,
        "end": end,
        "bucket_seconds": bucket_span,
        "series": series,
        "labels": labels,
    }


async def async_history_series(runtime, device_id, keys, hours, max_points, end=None):
    if end is not None and not math.isfinite(float(end)):
        raise ValueError("End time must be finite")
    end = min(time.time(), float(end)) if end is not None else time.time()
    device = runtime.data["devices"].get(device_id)
    if not device:
        raise ValueError("Unknown device")
    cache_key = (
        device_id,
        tuple(sorted(set(keys))),
        hours,
        max_points,
        end,
        device.get("label_revision", 0),
        device.get("training_state"),
        device.get("training_label_start"),
        device.get("training_expires_at"),
    )
    if cache_key in runtime._history_cache:
        runtime._history_cache.move_to_end(cache_key)
        return runtime._history_cache[cache_key]
    if cache_key not in runtime._history_jobs:
        view = runtime._history_view(device_id)

        async def build():
            try:
                result = await runtime.hass.async_add_executor_job(
                    view.history_series_multi, device_id, keys, hours, max_points, end
                )
                runtime._history_cache[cache_key] = result
                while len(runtime._history_cache) > 16:
                    runtime._history_cache.popitem(last=False)
                return result
            finally:
                runtime._history_jobs.pop(cache_key, None)

        runtime._history_jobs[cache_key] = asyncio.create_task(build())
    return await asyncio.shield(runtime._history_jobs[cache_key])


def _aggregate_buckets(runtime, device, start, end, bucket_span, indices, counts, buckets):
    for ts, row in runtime._iter_history_samples(device, start):
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


def _chart_labels(device, start, end):
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
            labels.append(
                {
                    "start": max(start, active_start),
                    "end": active_end,
                    "state": device.get("training_state", "unknown"),
                }
            )
    return labels
