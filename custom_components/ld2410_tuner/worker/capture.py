"""Executor-only acquisition of a bounded, portable numerical job."""

import math
from dataclasses import dataclass
from pathlib import Path

from ..history import dayfile, daystore, worker_snapshot
from . import codec, history, protocol

ATTRIBUTES = (
    "_history_until",
    "_snapshot_history_start",
    "_physical_history_start",
    "_attribution_since",
    "_attribution_until",
)


@dataclass(frozen=True)
class CaptureLimits:
    max_devices: int = 16
    max_days: int = 512
    max_rows: int = 1048576
    max_guesses: int = 32768
    max_raw_bytes: int = 32 * 1024 * 1024
    max_file_bytes: int = 512 * 1024 * 1024

    def __post_init__(self):
        if any(type(value) is not int or value <= 0 for value in vars(self).values()):
            raise ValueError("Capture limits must be positive integers")


def _dates(view, limits):
    store = getattr(view, "_days", None)
    devices = view.data["devices"]
    if len(devices) > limits.max_devices:
        raise worker_snapshot.SnapshotTooLarge("Worker device budget exceeded")
    found = []
    if store is None:
        return found
    start = getattr(view, "_snapshot_history_start", -math.inf)
    until = getattr(view, "_history_until", math.inf)
    for device_id in devices:
        found.extend(_device_dates(store, device_id, start, until, limits.max_days - len(found)))
    return found


def _device_dates(store, device_id, start, until, remaining):
    found = []
    for date in store.days(device_id):
        low, high = daystore.bounds(date)
        if high > start and low < until:
            found.append((device_id, date))
            if len(found) > remaining:
                raise worker_snapshot.SnapshotTooLarge("Worker day budget exceeded")
    return found


def _preflight(store, dates, limits):
    rows = guesses = raw = 0
    with store._lock:
        for device_id, date in dates:
            live = store._live.get(device_id)
            item = (
                live
                if live is not None and live.date == date
                else store._closing.get(device_id, {}).get(date)
            )
            late = store._late.get(device_id, {}).get(date, ())
            builder = None if item is None else item.builder
            counts = _builder_counts(builder)
            rows += counts[0]
            guesses += counts[1]
            raw += counts[2]
            guesses += len(late)
            if rows > limits.max_rows or guesses > limits.max_guesses or raw > limits.max_raw_bytes:
                raise worker_snapshot.SnapshotTooLarge("Aggregate mutable history budget exceeded")
    return rows, guesses, raw


def _builder_counts(builder):
    if builder is None:
        return 0, 0, 0
    raw = sum(
        len(column) * getattr(column, "itemsize", 1)
        for column in (builder.times, builder.ends, *builder.columns)
    )
    guesses = len(builder.guesses) + len(builder.confirmed) + (builder.run is not None)
    return len(builder.times), guesses, raw


def _entry(snapshot, device_id, date, directory, index):
    reference = snapshot.file
    if snapshot.day is not None:
        path = directory / f"{index}.day"
        path.write_bytes(dayfile.encode_day(snapshot.day))
        reference = worker_snapshot._reference(path)
    return {
        "device_id": device_id,
        "date": date.isoformat(),
        "persistent": snapshot.day is None and reference is not None,
        "path": None if reference is None else str(reference.path.resolve()),
        "identity": None if reference is None else [str(value) for value in reference.identity],
        "late": [
            [span.time, span.start, span.end, span.state, span.level, span.basis]
            for span in snapshot.late_guesses
        ],
    }


def prepare(view, device_id, directory, *, limits=None, _shared=None, _remaining=None):
    """Capture live columns; retain identity-checked references to finished files.

    The caller owns the private directory until its child has been reaped and
    validates evidence ownership before accepting a result.
    """
    worker_snapshot._executor_only()
    limits = CaptureLimits() if limits is None else limits
    data = codec.encode(view.data)
    protocol.pack("learn", {"data": data})
    dates = _dates(view, limits)
    store = getattr(view, "_days", None)
    if store is not None:
        _preflight(store, dates, limits)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    entries = _capture_days(view, dates, directory, limits, _shared, _remaining)
    attributes = {key: getattr(view, key) for key in ATTRIBUTES if hasattr(view, key)}
    attributes = {
        key: value for key, value in attributes.items() if value is None or math.isfinite(value)
    }
    request = {
        "device_id": device_id,
        "data": data,
        "attributes": attributes,
        "days": entries,
        "budgets": vars(limits),
        "live_dates": {
            key: str(store.live_date(key))
            for key in view.data["devices"]
            if store is not None and store.live_date(key) is not None
        },
    }
    protocol.pack("learn", request)
    return request


def _capture_days(view, dates, directory, limits, shared, remaining):
    store = getattr(view, "_days", None)
    entries, file_bytes = [], 0
    shared = {} if shared is None else shared
    remaining = (
        {"rows": limits.max_rows, "guesses": limits.max_guesses, "raw": limits.max_raw_bytes}
        if remaining is None
        else remaining
    )
    for key, date in dates:
        mark = id(store), key, date, getattr(view, "_history_until", None)
        entry = shared.get(mark)
        if entry is None:
            snapshot = _acquire(store, key, date, view, remaining)
            entry = _entry(snapshot, key, date, directory, len(shared))
            shared[mark] = entry
        file_bytes += 0 if entry["identity"] is None else int(entry["identity"][3])
        if file_bytes > limits.max_file_bytes:
            raise worker_snapshot.SnapshotTooLarge("Aggregate history file budget exceeded")
        entries.append(entry)
    return entries


def prepare_multi(views, directory, *, limits=None):
    """Capture a job's views once per distinct physical day and cutoff."""
    worker_snapshot._executor_only()
    limits = CaptureLimits() if limits is None else limits
    if len(views) > limits.max_devices:
        raise worker_snapshot.SnapshotTooLarge("Worker view budget exceeded")
    unique, stores = {}, {}
    for view in views.values():
        store = getattr(view, "_days", None)
        for device_id, date in _dates(view, limits):
            unique[(id(store), device_id, date)] = (device_id, date)
            stores[id(store)] = store
    if len(unique) > limits.max_days:
        raise worker_snapshot.SnapshotTooLarge("Aggregate worker day budget exceeded")
    devices = {key for _, key, _ in unique}
    if len(devices) > limits.max_devices:
        raise worker_snapshot.SnapshotTooLarge("Aggregate worker device budget exceeded")
    totals = [0, 0, 0]
    for store_id, store in stores.items():
        counts = _preflight(
            store, [item for mark, item in unique.items() if mark[0] == store_id], limits
        )
        totals = [a + b for a, b in zip(totals, counts, strict=True)]
    if any(
        a > b
        for a, b in zip(
            totals, (limits.max_rows, limits.max_guesses, limits.max_raw_bytes), strict=True
        )
    ):
        raise worker_snapshot.SnapshotTooLarge("Aggregate mutable history budget exceeded")
    shared = {}
    remaining = {
        "rows": limits.max_rows,
        "guesses": limits.max_guesses,
        "raw": limits.max_raw_bytes,
    }
    captured = {
        key: prepare(view, key, directory, limits=limits, _shared=shared, _remaining=remaining)
        for key, view in views.items()
    }
    total = sum(
        int(item["identity"][3]) for item in shared.values() if item["identity"] is not None
    )
    if total > limits.max_file_bytes:
        raise worker_snapshot.SnapshotTooLarge("Aggregate worker file budget exceeded")
    result = {"views": captured}
    protocol.pack("learn", result)
    return result


def _acquire(store, key, date, view, remaining):
    snapshot = worker_snapshot.acquire(
        store,
        key,
        date,
        getattr(view, "_history_until", None),
        limits=worker_snapshot.Limits(
            min(history.DAY_ROWS, remaining["rows"]),
            min(4096, remaining["guesses"]),
            min(history.DAY_BYTES, remaining["raw"]),
        ),
    )
    remaining["guesses"] -= len(snapshot.late_guesses)
    if snapshot.day is not None:
        remaining["rows"] -= len(snapshot.day.times)
        remaining["guesses"] -= len(snapshot.day.guesses)
        remaining["raw"] -= sum(
            len(column) * getattr(column, "itemsize", 1)
            for column in (snapshot.day.times, snapshot.day.ends, *snapshot.day.columns)
        )
    return snapshot
