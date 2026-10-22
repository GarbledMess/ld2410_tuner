"""Charts operations on the shared runtime state."""

from __future__ import annotations

import asyncio
import math
import time
from array import array
from bisect import bisect_left, bisect_right
from itertools import compress, pairwise, repeat
from operator import eq, gt, lt, mul, ne, sub
from typing import Any

from ..calibration.constants import SAMPLE_SECONDS
from ..const import HISTORY_KEYS
from ..history import pace, spans
from ..history.dayfile import MISSING
from ..history.policy import settings
from ..worker import bridge
from ..worker.supervisor import Busy, owned_preparation

MAX_HISTORY_JOBS = 2
# A fragmented radar-day chart is up to ~190 KB compressed; four fit the 1 MiB result.
ROOM_CHART_DEVICES = 4


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
    cache_key = _history_cache_key(device_id, device, keys, hours, max_points, end, summary)
    if cache_key in runtime._history_cache:
        runtime._history_cache.move_to_end(cache_key)
        return runtime._history_cache[cache_key]
    if cache_key not in runtime._history_jobs:
        # Keep only two lightweight refresh jobs outside the shared worker queue.
        # Exact-end requests remain distinct; repeated requests share their task.
        if len(runtime._history_jobs) >= MAX_HISTORY_JOBS:
            raise Busy("Chart refresh is busy; retry shortly")

        def prepare():
            view = _chart_view(runtime, device_id, hours, end)
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


def _history_cache_key(device_id, device, keys, hours, max_points, end, summary=False):
    return (
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


def _chart_view(runtime, device_id, hours, end):
    view = runtime._history_view(device_id, members=False)
    requested_hours = max(0.1, min(settings(runtime.data)["human_days"] * 24, float(hours)))
    view._capture_chart_window = True
    view._snapshot_history_start = end - requested_hours * 3600
    view._history_until = end
    return view


def _room_request(device_ids, keys, hours, end):
    if not isinstance(device_ids, list) or not 1 <= len(device_ids) <= ROOM_CHART_DEVICES:
        raise ValueError(f"Expected 1..{ROOM_CHART_DEVICES} unique device ids")
    if any(not isinstance(item, str) for item in device_ids) or len(set(device_ids)) != len(
        device_ids
    ):
        raise ValueError("Expected unique string device ids")
    if not any(key in HISTORY_KEYS for key in keys):
        raise ValueError("No valid gate keys given")
    if not math.isfinite(float(hours)):
        raise ValueError("Hours must be finite")
    if end is not None and not math.isfinite(float(end)):
        raise ValueError("End time must be finite")
    return min(time.time(), float(end)) if end is not None else time.time()


async def async_room_series(runtime, device_ids, keys, hours, max_points=400, end=None):
    """Build room charts in one display job, reusing per-device cached charts."""
    end = _room_request(device_ids, keys, hours, end)
    result = {"charts": {}, "errors": {}}
    misses = {}
    for device_id in device_ids:
        device = runtime.data["devices"].get(device_id)
        if device is None:
            result["errors"][device_id] = "Unknown device"
            continue
        key = _history_cache_key(device_id, device, keys, hours, max_points, end)
        if key in runtime._history_cache:
            runtime._history_cache.move_to_end(key)
            result["charts"][device_id] = runtime._history_cache[key]
        else:
            misses[device_id] = key
    if misses:
        built = await _room_history_job(runtime, misses, keys, hours, max_points, end)
        result["charts"].update(built["charts"])
        result["errors"].update(built["errors"])
    return result


async def _room_history_job(runtime, misses, keys, hours, max_points, end):
    job_key = tuple(misses.values())
    if job_key not in runtime._history_jobs:
        if len(runtime._history_jobs) >= MAX_HISTORY_JOBS:
            raise Busy("Chart refresh is busy; retry shortly")

        def prepare():
            return {
                "views": {
                    device_id: _chart_view(runtime, device_id, hours, end) for device_id in misses
                },
                "parameters": {
                    "device_ids": list(misses),
                    "keys": keys,
                    "hours": hours,
                    "max_points": max_points,
                    "end": end,
                },
            }

        task = asyncio.create_task(_build_room_series(runtime, job_key, misses, prepare))
        runtime._history_jobs[job_key] = task
        task.add_done_callback(_history_done)
    return await asyncio.shield(runtime._history_jobs[job_key])


async def _build_room_series(runtime, job_key, misses, prepare):
    from ..worker.display import unpack_charts

    try:
        result = await bridge.calculate(runtime, "charts", prepare)
        result = await owned_preparation(
            runtime.hass.async_add_executor_job(unpack_charts, result, list(misses))
        )
        for device_id, chart in result["charts"].items():
            runtime._history_cache[misses[device_id]] = chart
        while len(runtime._history_cache) > 16:
            runtime._history_cache.popitem(last=False)
        return result
    finally:
        runtime._history_jobs.pop(job_key, None)


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
    modes = _column_chart(runtime, device_id, indices, start, end, series, strongest)
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


def _column_chart(runtime, device_id, indices, start, end, series, strongest):
    """Feed each series the runs of its column instead of one rebuilt vector per row.

    A run is consecutive contiguous rows holding the same value, so the series see the
    same values over the same physical time as per-row spans, in far fewer pieces.
    """
    start = max(start, getattr(runtime, "_physical_history_start", -math.inf))
    end = min(end, getattr(runtime, "_attribution_until", math.inf))
    if end <= start:
        return set()
    observed, last_end = False, -math.inf
    for day, selected in spans._selected_days(runtime, device_id, start, end):
        for low, high in selected:
            rows = _Rows.select(day, low, high, last_end)
            if rows is not None:
                last_end = day.ends[rows.after - 1]
                observed |= _chart_rows(rows, indices, series, strongest)
    return {"event"} if observed else set()


class _Rows:
    """Rows ``[first, after)`` of one day, clipped to ``[low, high)``."""

    def __init__(self, day, first, after, low, high):
        self.day, self.first, self.after, self.low, self.high = day, first, after, low, high
        times, ends = day.times[first + 1 : after], day.ends[first : after - 1]
        # Offsets (from ``first``) of rows that start after a recording gap.
        self.breaks = set(compress(range(1, after - first), map(ne, times, ends)))

    @classmethod
    def select(cls, day, low, high, last_end):
        first, after = bisect_right(day.ends, low), bisect_left(day.times, high)
        if first >= after:
            return None
        if day.times[first] < last_end:
            raise ValueError("Physical history contains overlapping coverage")
        return cls(day, first, after, low, high)

    def column(self, position):
        return self.day.columns[position][self.first : self.after]


def _chart_rows(rows, indices, series, strongest):
    if strongest is not None:
        columns = {None: _strongest_column([rows.column(p) for p in indices.values()])}
    else:
        columns = {key: rows.column(position) for key, position in indices.items()}
    day, first, after = rows.day, rows.first, rows.after
    durations = list(map(sub, day.ends[first:after], day.times[first:after]))
    zeros = list(compress(range(len(durations)), map(eq, durations, repeat(0.0))))
    for key, column in columns.items():
        _add_column(rows, column, strongest if key is None else series[key], durations, zeros)
    return any(durations)


def _add_column(rows, column, target, durations, zeros):
    for low, high, chunk in _chunks(rows, column, target.bucket_span):
        pace.rest()
        if bisect_left(zeros, chunk[1]) > bisect_left(zeros, chunk[0]):
            _add_runs(rows, column, target, low, high, *chunk[:2])
        else:
            target.add_chunk(low, high, _summary(rows, column, durations, low, high, *chunk))


def _add_runs(rows, column, target, low, high, first, after):
    """A zero-length reading splits value runs: keep the exact run path."""
    sub_rows = _Rows(rows.day, rows.first + first, rows.first + after, low, high)
    for run_start, run_end, value in _runs(sub_rows, column[first:after]):
        if value is not None:
            target.add(run_start, run_end, value)


def _chunks(rows, column, bucket_span):
    """``(low, high, (first, after, changes))`` of known contiguous rows within one bucket.

    Missing readings and recording gaps end a stretch; buckets split it as ``add`` does.
    """
    day, first = rows.day, rows.first
    missing = _missing(column)
    changes = list(compress(range(1, len(column)), map(ne, column[1:], column[:-1])))
    cuts = sorted({0, len(column), *rows.breaks, *missing, *(m + 1 for m in missing)})
    missing = set(missing)
    for begin, stop in pairwise(cuts):
        if begin in missing:
            continue
        start = max(rows.low, day.times[first + begin])
        end = min(rows.high, day.ends[first + stop - 1])
        while start < end:
            high = min(end, (math.floor(start / bucket_span) + 1) * bucket_span)
            low_row = bisect_right(day.ends, start, first + begin, first + stop) - first
            high_row = bisect_left(day.times, high, first + begin, first + stop) - first
            yield start, high, (low_row, high_row, changes)
            start = high


def _missing(column):
    if isinstance(column, bytes):
        return list(compress(range(len(column)), map(eq, column, repeat(MISSING))))
    return list(compress(range(len(column)), map(math.isnan, column)))


def _summary(rows, column, durations, low, high, first, after, changes):
    """One bucket's known contiguous rows as the accumulator their runs would build."""
    day, base = rows.day, rows.first
    values = column[first:after]

    def run_end(offset):
        stop = bisect_right(changes, offset)
        stop = changes[stop] if stop < len(changes) and changes[stop] < after else after
        return min(high, day.ends[base + stop - 1])

    def extremum(value):
        offset = first + values.index(value)
        return value, max(low, day.times[base + offset]), run_end(offset)

    last = bisect_right(changes, after - 1) - 1
    last = max(first, changes[last]) if last >= 0 else first
    integral = (
        sum(map(mul, values, durations[first:after]))
        - values[0] * (low - day.times[base + first])
        - values[-1] * (day.ends[base + after - 1] - high)
    )
    return {
        "min": extremum(min(values)),
        "max": extremum(max(values)),
        "first": (values[0], run_end(first)),
        "last": (values[-1], max(low, day.times[base + last])),
        "integral": integral,
    }


def _strongest_column(columns):
    """Per-row maximum of the selected gates; missing only where all of them are."""
    if all(isinstance(column, bytes) for column in columns):
        if len(columns) == 1:
            return columns[0]
        # Missing (255) reads as 0 for the maximum; rows with every gate missing stay 255.
        present = bytes(map(max, *(column.translate(_MISSING_AS_ZERO) for column in columns)))
        absent = bytes(map(min, *columns)).translate(_ONLY_MISSING)
        return bytes(map(max, present, absent))
    return array(
        "d",
        (
            max(filter(_known, row), default=math.nan)
            for row in zip(*map(_floats, columns), strict=True)
        ),
    )


def _floats(column):
    if not isinstance(column, bytes):
        return column
    return [math.nan if value == MISSING else value for value in column]


def _known(value):
    return not math.isnan(value)


_MISSING_AS_ZERO = bytes(range(MISSING)) + b"\0"
_ONLY_MISSING = bytes(MISSING) + bytes((MISSING,))


def _runs(rows, column):
    """``(start, end, value)`` of each run of equal values between recording gaps."""
    changes = set(compress(range(1, len(column)), map(ne, column[1:], column[:-1])))
    begin = 0
    for stop in (*sorted(changes | rows.breaks), len(column)):
        pace.rest()
        run_start = max(rows.low, rows.day.times[rows.first + begin])
        run_end = min(rows.high, rows.day.ends[rows.first + stop - 1])
        if run_end > run_start:
            yield run_start, run_end, _value(column[begin])
        begin = stop


def _value(value):
    if value == MISSING or math.isnan(value):
        return None
    return value


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
            high = min(end, (self._enter(start) + 1) * self.bucket_span)
            self._piece(start, high, value)
            start = high

    def add_chunk(self, start, end, summary):
        """Contiguous runs within one bucket, summarised by ``_summary``."""
        if summary["min"][0] == summary["max"][0]:
            self.add(start, end, summary["min"][0])  # One run: exactly a single piece.
            return
        self._enter(start)
        self._piece(start, end, summary, _chunk_accumulator, _merge_accumulator)

    def _enter(self, start):
        bucket = math.floor(start / self.bucket_span)
        if self.bucket != bucket:
            self._flush()
            self.bucket = bucket
            self.segments, self.overfull = [], False
        return bucket

    def _piece(self, start, end, value, new=None, extend=None):
        self._gap(self.last_end, start)
        self.last_end = end
        self.observed += end - start
        if self.overfull:
            return
        if not self.segments or self.segments[-1]["end"] < start:
            self.segments.append((new or _new_accumulator)(start, end, value))
        else:
            (extend or _extend_accumulator)(self.segments[-1], start, end, value)
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


def _chunk_accumulator(start, end, summary):
    acc = _new_accumulator(start, end, summary["first"][0])
    acc["min"], acc["min_start"], acc["min_end"] = summary["min"]
    acc["max"], acc["max_start"], acc["max_end"] = summary["max"]
    _chunk_integral(acc, end, summary, 0.0)
    return acc


def _merge_accumulator(acc, start, end, summary):
    """Apply a chunk's runs in order: first run may extend, a new extremum replaces."""
    first, first_end = summary["first"]
    for name, better in (("min", lt), ("max", gt)):
        value, low, high = summary[name]
        if better(value, acc[name]):
            acc[name], acc[name + "_start"], acc[name + "_end"] = value, low, high
        elif first == acc[name] and start == acc[name + "_end"]:
            acc[name + "_end"] = first_end
    acc["end"] = end
    acc["observed_seconds"] = end - acc["start"]
    _chunk_integral(acc, end, summary, acc["integral"])


def _chunk_integral(acc, end, summary, before):
    value, start = summary["last"]
    acc["integral"] = before + summary["integral"]
    acc["_last_value"], acc["_last_start"] = value, start
    acc["_closed_integral"] = acc["integral"] - value * (end - start)


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
