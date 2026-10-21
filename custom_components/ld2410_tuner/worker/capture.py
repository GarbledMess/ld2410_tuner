"""Executor-only acquisition of a bounded, portable numerical job."""

import math
from dataclasses import dataclass
from pathlib import Path

from ..history import dayfile, daystore, worker_snapshot
from . import codec, protocol

ATTRIBUTES = (
    "_history_until",
    "_snapshot_history_start",
    "_physical_history_start",
    "_attribution_since",
    "_attribution_until",
)


@dataclass(frozen=True)
class CaptureLimits:
    max_devices: int | None = None
    max_days: int | None = None
    max_rows: int | None = None
    max_guesses: int | None = None
    max_raw_bytes: int = 32 * 1024 * 1024
    max_file_bytes: int | None = None

    def __post_init__(self):
        if self.max_raw_bytes is None or any(
            value is not None and (type(value) is not int or value <= 0)
            for value in vars(self).values()
        ):
            raise ValueError("Capture limits must be positive integers")


def _dates(view, limits):
    store = getattr(view, "_days", None)
    devices = view.data["devices"]
    if limits.max_devices is not None and len(devices) > limits.max_devices:
        raise worker_snapshot.SnapshotTooLarge("Worker device budget exceeded")
    found = []
    if store is None:
        return found
    start = getattr(view, "_snapshot_history_start", -math.inf)
    until = getattr(view, "_history_until", math.inf)
    for device_id in devices:
        found.extend(
            _device_dates(
                store,
                device_id,
                start,
                until,
                None if limits.max_days is None else limits.max_days - len(found),
            )
        )
    return found


def _device_dates(store, device_id, start, until, remaining):
    found = []
    for date in store.days(device_id):
        low, high = daystore.bounds(date)
        if high > start and low < until:
            found.append((device_id, date))
            if remaining is not None and len(found) > remaining:
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
            raw += len(late) * 256
            if any(
                bound is not None and actual > bound
                for actual, bound in zip(
                    (rows, guesses, counts[2] + len(late) * 256),
                    (limits.max_rows, limits.max_guesses, limits.max_raw_bytes),
                    strict=True,
                )
            ):
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
    return len(builder.times), guesses, raw + guesses * 256


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


def prepare(view, device_id, directory, *, limits=None, _shared=None, _remaining=None, sink=None):
    """Capture live columns; retain identity-checked references to finished files.

    The caller owns the private directory until its child has been reaped and
    validates evidence ownership before accepting a result.
    """
    worker_snapshot._executor_only()
    limits = CaptureLimits() if limits is None else limits
    data = codec.encode(view.data)
    if sink is None:
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
    if sink is not None:
        sink(device_id, request)
        return None
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
        if limits.max_file_bytes is not None and file_bytes > limits.max_file_bytes:
            raise worker_snapshot.SnapshotTooLarge("Aggregate history file budget exceeded")
        entries.append(entry)
    return entries


def prepare_multi(views, directory, *, limits=None, sink=None):
    """Capture a job's views once per distinct physical day and cutoff."""
    worker_snapshot._executor_only()
    limits = CaptureLimits() if limits is None else limits
    if limits.max_devices is not None and len(views) > limits.max_devices:
        raise worker_snapshot.SnapshotTooLarge("Worker view budget exceeded")
    _preflight_multi(views, limits)
    shared = {}
    remaining = {
        "rows": limits.max_rows,
        "guesses": limits.max_guesses,
        "raw": limits.max_raw_bytes,
    }
    captured = {
        key: prepare(
            view, key, directory, limits=limits, _shared=shared, _remaining=remaining, sink=sink
        )
        for key, view in views.items()
    }
    total = sum(
        int(item["identity"][3]) for item in shared.values() if item["identity"] is not None
    )
    if limits.max_file_bytes is not None and total > limits.max_file_bytes:
        raise worker_snapshot.SnapshotTooLarge("Aggregate worker file budget exceeded")
    if sink is not None:
        return None
    result = {"views": captured}
    protocol.pack("learn", result)
    return result


def _unique_days(views, limits):
    unique, stores = {}, {}
    for view in views.values():
        store = getattr(view, "_days", None)
        for device_id, date in _dates(view, limits):
            unique[(id(store), device_id, date)] = (device_id, date)
            stores[id(store)] = store
    return unique, stores


def _preflight_multi(views, limits):
    unique, stores = _unique_days(views, limits)
    if limits.max_days is not None and len(unique) > limits.max_days:
        raise worker_snapshot.SnapshotTooLarge("Aggregate worker day budget exceeded")
    devices = {key for _, key, _ in unique}
    if limits.max_devices is not None and len(devices) > limits.max_devices:
        raise worker_snapshot.SnapshotTooLarge("Aggregate worker device budget exceeded")
    totals = [0, 0, 0]
    for store_id, store in stores.items():
        counts = _preflight(
            store, [item for mark, item in unique.items() if mark[0] == store_id], limits
        )
        totals = [a + b for a, b in zip(totals, counts, strict=True)]
    if any(
        b is not None and a > b
        for a, b in zip(totals[:2], (limits.max_rows, limits.max_guesses), strict=True)
    ):
        raise worker_snapshot.SnapshotTooLarge("Aggregate mutable history budget exceeded")


def _acquire(store, key, date, view, remaining):
    snapshot = worker_snapshot.acquire(
        store,
        key,
        date,
        getattr(view, "_history_until", None),
        limits=worker_snapshot.Limits(
            remaining["rows"],
            remaining["guesses"],
            remaining["raw"],
        ),
    )
    if remaining["guesses"] is not None:
        remaining["guesses"] -= len(snapshot.late_guesses)
    if snapshot.day is not None:
        if remaining["rows"] is not None:
            remaining["rows"] -= len(snapshot.day.times)
        if remaining["guesses"] is not None:
            remaining["guesses"] -= len(snapshot.day.guesses)
    return snapshot
