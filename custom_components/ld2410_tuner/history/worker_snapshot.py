"""Executor-only, bounded acquisition for disposable history workers.

Limits bound raw columns and the full builder inputs visited by ``view``, even
when a requested cutoff selects a tiny prefix. They are not an RSS guarantee.
Finished files are references, never read or decoded here: child-side decoding
limits and post-work identity/revision validation remain the caller's duty.
"""

import asyncio
import math
import threading
import time
from array import array
from dataclasses import dataclass, replace
from pathlib import Path

from . import dayfile


class SnapshotTooLarge(ValueError):
    """Acquisition would exceed an explicit allocation/work budget."""


@dataclass(frozen=True)
class Limits:
    max_rows: int = 32768
    max_guesses: int = 4096
    max_raw_bytes: int = 4 * 1024 * 1024

    def __post_init__(self):
        for value in (self.max_rows, self.max_guesses, self.max_raw_bytes):
            if type(value) is not int or value < 0:
                raise ValueError("Snapshot limits must be nonnegative integers")


@dataclass(frozen=True)
class FileReference:
    path: Path
    identity: tuple


@dataclass(frozen=True)
class Snapshot:
    day: dayfile.Day | None
    file: FileReference | None
    late_guesses: tuple
    generation: int
    rewrites: int
    revision: int
    changes: int
    cutoff: float


def _executor_only():
    if threading.current_thread() is threading.main_thread():
        raise RuntimeError("Worker snapshot acquisition requires an executor")
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError("Worker snapshot acquisition requires an executor")


def _preflight(builder, late, limits):
    rows, guesses, raw = 0, len(late), 0
    if builder is not None:
        rows = len(builder.times)
        guesses += len(builder.guesses) + len(builder.confirmed) + (builder.run is not None)
        raw = (
            len(builder.times) * builder.times.itemsize + len(builder.ends) * builder.ends.itemsize
        )
        raw += sum(len(column) * getattr(column, "itemsize", 1) for column in builder.columns)
    if rows > limits.max_rows or guesses > limits.max_guesses or raw > limits.max_raw_bytes:
        raise SnapshotTooLarge("Worker snapshot exceeds row, guess or raw-column-byte limit")


def _reference(path):
    try:
        found = path.stat()
    except FileNotFoundError:
        return None
    return FileReference(path, (found.st_dev, found.st_ino, found.st_mtime_ns, found.st_size))


def _detached(builder, cutoff, closing):
    day = builder.view(cutoff, math.inf) if closing else builder.view(cutoff)
    del day._summary_owner
    del day._summary_version
    return day


def _cutoff(live, current, until, start, end):
    if not current:
        return end
    limit = max(time.time(), live.seen) if until is None else until
    return min(max(limit, start), end)


def acquire(store, device_id, date, until=None, *, limits=None):
    """Capture mutable evidence/markers under the lock; stat finished files outside it.

    Live cutoffs follow ``DayStore.day``; closing days finish at midnight and
    retain all confirmed spans, as that read does. Pending earlier-day spans are
    separate: consumers must splice them after decoding/copying the base day.
    Missing files yield no reference; other stat failures propagate. A reference
    may disappear or be replaced afterwards and must be revalidated by callers.
    File identity and captured store markers are not one atomic filesystem view.
    """
    from .daystore import bounds

    _executor_only()
    limits = Limits() if limits is None else limits
    start, end = bounds(date)
    with store._lock:
        live = store._live.get(device_id)
        current = live is not None and live.date == date
        closing = None if current else store._closing.get(device_id, {}).get(date)
        builder = live.builder if current else None if closing is None else closing.builder
        late = store._late.get(device_id, {}).get(date, ())
        _preflight(builder, late, limits)
        cutoff = _cutoff(live, current, until, start, end)
        day = None if builder is None else _detached(builder, cutoff, closing is not None)
        if builder is None:
            day = _accepted_day(store, device_id, date, day, late, limits, start, end)
        snapshot = Snapshot(
            day,
            None,
            tuple(late),
            store._generations.get(device_id, 0),
            store._rewrites.get(device_id, 0),
            store._revision.get(device_id, 0),
            store._changes.get((device_id, date), 0),
            cutoff,
        )
    # Slow storage must not hold the recording lock while gathering file metadata.
    return (
        replace(snapshot, file=_reference(store._path(device_id, date, ".day")))
        if day is None
        else snapshot
    )


def _preflight_day(day, late, limits):
    raw = len(day.times) * day.times.itemsize + len(day.ends) * day.ends.itemsize
    raw += sum(len(column) * getattr(column, "itemsize", 1) for column in day.columns)
    if (
        len(day.times) > limits.max_rows
        or len(day.guesses) + len(late) > limits.max_guesses
        or raw > limits.max_raw_bytes
    ):
        raise SnapshotTooLarge("Worker snapshot exceeds row, guess or raw-column-byte limit")


def _accepted_day(store, device_id, date, day, late, limits, start, end):
    if device_id in store._clear_pending:
        return dayfile.DayBuilder(start, end).day()
    pending = store._pending.get((device_id, date))
    if pending is None:
        return day
    accepted = pending[0]
    _preflight_day(accepted, late, limits)
    return dayfile.Day(
        accepted.start,
        accepted.end,
        array("d", accepted.times),
        array("d", accepted.ends),
        [column[:] for column in accepted.columns],
        list(accepted.guesses),
    )
