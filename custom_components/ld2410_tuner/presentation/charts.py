"""Charts operations on the shared runtime state."""

from __future__ import annotations

import asyncio
import math
import time
from typing import Any

from ..calibration.constants import SAMPLE_SECONDS
from ..const import HISTORY_KEYS
from ..history import pace, spans
from ..history.policy import settings
from ..worker import bridge
from ..worker.supervisor import Busy, owned_preparation

MAX_HISTORY_JOBS = 2


@pace.background
def history_series_multi(
    runtime,
    device_id: str,
    keys: list[str],
    hours: float,
    max_points: int = 400,
    end: float | None = None,
    summary: bool = False,
) -> dict[str, Any]:
    """Downsampled time series for several gates at once, sharing one pass
    over the history and one set of labels, so the chart can overlay
    every gate of a kind (move or still) together instead of one at a
    time.

    Returns min/avg/max per bucket per gate (rather than a single
    averaged value) so short spikes during a quiet period aren't
    smoothed away - those spikes are exactly what matters when judging
    whether a threshold is safely above the real noise ceiling.

    A room summary first takes the maximum of the selected gates in each
    physical span, then applies the same duration-weighted aggregation. Its
    extrema retain their physical intervals, and coverage is the union of
    available selected gates rather than the coverage of a winning gate.
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
    hours = max(0.1, min(settings(runtime.data)["human_days"] * 24, float(hours)))
    max_points = max(50, min(1000, int(max_points)))
    if end is not None and not math.isfinite(float(end)):
        raise ValueError("End time must be finite")
    end = min(time.time(), float(end)) if end is not None else time.time()
    start = end - hours * 3600
    span = end - start

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
    return _native_chart(runtime, device_id, indices, start, end, bucket_span, max_points, summary)


async def async_history_series(
    runtime, device_id, keys, hours, max_points, end=None, summary=False
):
    if end is not None and not math.isfinite(float(end)):
        raise ValueError("End time must be finite")
    end = min(time.time(), float(end)) if end is not None else time.time()
    device = runtime.data["devices"].get(device_id)
    if not device:
        raise ValueError("Unknown device")
    cache_key = (
        device_id,
        tuple(dict.fromkeys(keys)) if summary else tuple(sorted(set(keys))),
        summary,
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
        # Keep only two lightweight refresh jobs outside the shared worker queue.
        # Exact-end requests remain distinct; repeated requests share their task.
        if len(runtime._history_jobs) >= MAX_HISTORY_JOBS:
            raise Busy("Chart refresh is busy; retry shortly")

        def prepare():
            view = runtime._history_view(device_id, members=False)
            requested_hours = max(0.1, min(settings(runtime.data)["human_days"] * 24, float(hours)))
            view._capture_chart_window = True
            view._snapshot_history_start = end - requested_hours * 3600
            view._history_until = end
            return {
                "views": {device_id: view},
                "parameters": {
                    "device_id": device_id,
                    "keys": keys,
                    "hours": hours,
                    "max_points": max_points,
                    "end": end,
                    "summary": summary,
                },
            }

        runtime._history_jobs[cache_key] = asyncio.create_task(
            _build_history_series(runtime, cache_key, prepare)
        )
        # A disconnected websocket may leave no waiter to retrieve a later failure.
        runtime._history_jobs[cache_key].add_done_callback(_history_done)
    return await asyncio.shield(runtime._history_jobs[cache_key])


async def _build_history_series(runtime, cache_key, prepare):
    try:
        result = await bridge.calculate(runtime, "charts", prepare)
        from ..worker.display import unpack_chart

        result = await owned_preparation(runtime.hass.async_add_executor_job(unpack_chart, result))
        runtime._history_cache[cache_key] = result
        while len(runtime._history_cache) > 16:
            runtime._history_cache.popitem(last=False)
        return result
    finally:
        runtime._history_jobs.pop(cache_key, None)


def _history_done(task):
    if not task.cancelled():
        task.exception()


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


def _native_chart(runtime, device_id, indices, start, end, bucket_span, max_points, summary=False):
    while 2 * (math.ceil(end / bucket_span) - math.floor(start / bucket_span)) > max_points:
        bucket_span *= 2
    series = (
        {}
        if summary
        else {key: _NativeSeries(start, end, bucket_span, max_points) for key in indices}
    )
    strongest = _NativeSeries(start, end, bucket_span, max_points) if summary else None
    modes = set()
    for cell in spans.events(runtime, device_id, start=start, end=end):
        pace.rest()
        modes.add(cell.mode)
        _add_native_span(series, strongest, indices, cell)
    if strongest is not None:
        return {
            "start": start,
            "end": end,
            "bucket_seconds": bucket_span,
            "points": strongest.finish()["points"],
        }
    return {
        "start": start,
        "end": end,
        "bucket_seconds": bucket_span,
        "series": {key: value.finish() for key, value in series.items()},
        "labels": _chart_labels(runtime.data["devices"][device_id], start, end),
        "basis": "physical_span_duration",
        "modes": sorted(modes),
    }


def _add_native_span(series, strongest, indices, cell):
    if strongest is not None:
        values = [
            cell.values[position]
            for position in indices.values()
            if cell.values[position] is not None
        ]
        if values:
            strongest.add(cell.start, cell.end, max(values))
        return
    for key, position in indices.items():
        value = cell.values[position]
        if value is not None:
            series[key].add(cell.start, cell.end, value)


class _NativeSeries:
    """Bounded native plot segments; an overfull bucket is omitted, never bridged."""

    def __init__(self, start, end, bucket_span, limit):
        self.start, self.end, self.bucket_span, self.limit = start, end, bucket_span, limit
        self.points, self.gaps, self.omitted = [], [], []
        self.gaps_truncated = False
        self.observed, self.last_end = 0.0, start
        self.bucket, self.segments, self.overfull = None, [], False

    def add(self, start, end, value):
        while start < end:
            bucket = math.floor(start / self.bucket_span)
            high = min(end, (bucket + 1) * self.bucket_span)
            if self.bucket != bucket:
                self._flush()
                self.bucket = bucket
                self.segments, self.overfull = [], False
            self._piece(start, high, value)
            start = high

    def _piece(self, start, end, value):
        self._gap(self.last_end, start)
        self.last_end = end
        self.observed += end - start
        if self.overfull:
            return
        if not self.segments or self.segments[-1]["end"] < start:
            self.segments.append(_new_accumulator(start, end, value))
        else:
            _extend_accumulator(self.segments[-1], start, end, value)
        if len(self.points) + 2 * len(self.segments) > self.limit:
            self.segments, self.overfull = [], True

    def _flush(self):
        if self.bucket is None:
            return
        if self.overfull:
            low = max(self.start, self.bucket * self.bucket_span)
            high = min(self.end, (self.bucket + 1) * self.bucket_span)
            _append_range(self.omitted, low, high)
            return
        for acc in self.segments:
            self._anchors(acc)

    def _anchors(self, acc):
        point = {
            key: value
            for key, value in acc.items()
            if key != "integral" and not key.startswith("_")
        }
        point["avg"] = acc["integral"] / acc["observed_seconds"]
        point["count"] = acc["observed_seconds"] / SAMPLE_SECONDS
        point["t"] = acc["start"]
        point["break_before"] = bool(self.points and acc["start"] > self.points[-1]["end"])
        self.points.append(point)
        self.points.append(
            {
                **point,
                "t": acc["end"],
                "observed_seconds": 0.0,
                "count": 0.0,
                "anchor": True,
                "break_before": False,
            }
        )

    def _gap(self, start, end):
        if end <= start:
            return
        if len(self.gaps) < self.limit:
            _append_range(self.gaps, start, end)
        else:
            self.gaps_truncated = True

    def finish(self):
        self._flush()
        self._gap(self.last_end, self.end)
        return {
            "sample_count": self.observed / SAMPLE_SECONDS,
            "observed_seconds": self.observed,
            "basis": "physical_span_duration",
            "count_basis": "six_second_equivalent_duration",
            "points": self.points,
            "gaps": self.gaps,
            "gaps_truncated": self.gaps_truncated,
            "omitted_ranges": self.omitted,
        }


def _new_accumulator(start, end, value):
    return {
        "start": start,
        "end": end,
        "min": value,
        "max": value,
        "min_start": start,
        "min_end": end,
        "max_start": start,
        "max_end": end,
        "observed_seconds": end - start,
        "integral": value * (end - start),
        "_last_value": value,
        "_last_start": start,
        "_closed_integral": 0.0,
    }


def _extend_accumulator(acc, start, end, value):
    _extremum(acc, "min", start, end, value, value < acc["min"])
    _extremum(acc, "max", start, end, value, value > acc["max"])
    acc["end"] = end
    acc["observed_seconds"] = end - acc["start"]
    _value_integral(acc, start, end, value)


def _extremum(acc, name, start, end, value, replace):
    if replace:
        acc[name], acc[name + "_start"], acc[name + "_end"] = value, start, end
    elif value == acc[name] and start == acc[name + "_end"]:
        acc[name + "_end"] = end


def _append_range(intervals, start, end):
    if intervals and start == intervals[-1][1]:
        intervals[-1] = (intervals[-1][0], end)
    else:
        intervals.append((start, end))


def _value_integral(acc, start, end, value):
    if value != acc["_last_value"]:
        acc["_closed_integral"] = acc["integral"]
        acc["_last_value"], acc["_last_start"] = value, start
    acc["integral"] = acc["_closed_integral"] + value * (end - acc["_last_start"])
