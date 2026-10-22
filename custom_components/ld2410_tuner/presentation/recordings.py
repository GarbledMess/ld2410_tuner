"""Read-only bounded access to exact recording rows."""

import asyncio
import datetime
import math
import os
import stat
import time
from pathlib import Path
from types import SimpleNamespace

from ..history import daystore
from ..worker import bridge, priority
from ..worker.supervisor import Busy, OutputLimit, WorkerFailed, owned_preparation


def day_bounds(value):
    date = datetime.date.fromisoformat(value)
    if date.isoformat() != value:
        raise ValueError("Use a UTC date in YYYY-MM-DD format")
    return daystore.bounds(date)


def pagination(offset, limit, maximum):
    if type(offset) is not int or offset < 0:
        raise ValueError("Offset must be a nonnegative integer")
    if type(limit) is not int or not 1 <= limit <= maximum:
        raise ValueError(f"Limit must be between 1 and {maximum}")


def raw_view(runtime, device_id, date, end):
    start, _ = day_bounds(date)
    return SimpleNamespace(
        data={"devices": {device_id: {}}},
        _days=runtime._days,
        _snapshot_history_start=start,
        _physical_history_start=start,
        # Capture admits days by a half-open window; include stored midnight instants.
        _history_until=max(end, start + 0.001),
    )


def _device(runtime, device_id):
    if device_id not in runtime.data["devices"]:
        raise ValueError("Unknown device")
    if runtime._days is None:
        raise ValueError("Recordings are unavailable")
    return runtime._days


def _done(task):
    if not task.cancelled():
        task.exception()


async def _bounded(runtime, key, build):
    jobs = runtime._recording_jobs
    if key not in jobs:
        if len(jobs) >= 2:
            raise Busy("Recording browser is busy; retry shortly")
        jobs[key] = asyncio.create_task(_run(jobs, key, build))
        jobs[key].add_done_callback(_done)
    return await asyncio.shield(jobs[key])


async def _run(jobs, key, build):
    try:
        return await build()
    finally:
        jobs.pop(key, None)


async def async_recording_days(runtime, device_id=None, offset=0, limit=50):
    pagination(offset, limit, 100)
    if device_id is None:

        async def bootstrap():
            return await owned_preparation(
                runtime.hass.async_add_executor_job(recording_devices, runtime)
            )

        return await _bounded(runtime, ("devices",), bootstrap)
    store = _device(runtime, device_id)

    async def build():
        return await owned_preparation(
            runtime.hass.async_add_executor_job(_days, store, device_id, offset, limit)
        )

    return await _bounded(runtime, ("days", device_id, offset, limit), build)


def _size(path):
    try:
        return path.stat().st_size
    except FileNotFoundError:
        return 0


def _days(store, device_id, offset, limit):
    dates = sorted(store.days(device_id), reverse=True)
    live = store.live_date(device_id)
    entries = []
    total_bytes = 0
    for index, date in enumerate(dates):
        sizes = [_size(store._path(device_id, date, suffix)) for suffix in (".day", ".log", ".sum")]
        total_bytes += sum(sizes)
        if offset <= index < offset + limit:
            entries.append(
                {
                    "date": date.isoformat(),
                    "bytes": sum(sizes),
                    "day_bytes": sizes[0],
                    "log_bytes": sizes[1],
                    "index_bytes": sizes[2],
                    "live": date == live,
                }
            )
    after = offset + len(entries)
    return {
        "days": entries,
        "total": len(dates),
        "offset": offset,
        "next_offset": after if after < len(dates) else None,
        "bytes": total_bytes,
    }


async def async_recording_rows(
    runtime, device_id, date, offset=0, limit=100, end=None, revision=None
):
    store = _device(runtime, device_id)
    pagination(offset, limit, 10000)
    current = store.rewrites(device_id)
    if revision is not None and (type(revision) is not int or revision != current):
        raise ValueError("Recordings changed; reload the day")
    parameters = {
        "device_id": device_id,
        "date": date,
        "offset": offset,
        "limit": limit,
        "end": _cutoff(store, device_id, date, end),
        "revision": current,
    }
    return await _bounded(
        runtime,
        ("rows", *parameters.values()),
        lambda: _read_rows(runtime, store, parameters),
    )


def _cutoff(store, device_id, date, end):
    """The pinned end of a day's readings: the request's, else the latest observation."""
    start, stop = day_bounds(date)
    if end is not None:
        if not math.isfinite(float(end)) or not start <= end <= stop:
            raise ValueError("End time must lie within the UTC day")
        return end
    observed = store.seen(device_id)
    return min(stop, max(start, time.time() if observed is None else observed))


async def _read_rows(runtime, store, parameters):
    device_id, current = parameters["device_id"], parameters["revision"]

    def prepare():
        _check_revision(store, device_id, current)
        view = raw_view(runtime, device_id, parameters["date"], parameters["end"])
        return {"views": {device_id: view}, "parameters": parameters}

    # Export pages yield to page views and charts in the display worker queue.
    background = parameters["limit"] > 100
    calculate = priority.background(bridge.calculate) if background else bridge.calculate
    try:
        result = await calculate(runtime, "recording_rows", prepare)
    except (WorkerFailed, OutputLimit, TimeoutError) as error:
        raise ValueError("Recording day could not be read; retry or choose another day") from error
    from ..worker.recordings import unpack_page

    result = await owned_preparation(runtime.hass.async_add_executor_job(unpack_page, result))
    _check_revision(store, device_id, current)
    return result


def _check_revision(store, device_id, expected):
    if store.rewrites(device_id) != expected:
        raise ValueError("Recordings changed; reload the day")


def recording_devices(runtime):
    store = runtime._days
    result = []
    if store is not None:
        for device_id, device in list(runtime.data["devices"].items()):
            summary = _days(store, device_id, 0, 1)
            if summary["total"]:
                result.append(
                    {
                        "id": device_id,
                        "name": device.get("name") or "Archived radar " + device_id[-8:],
                        "days": summary["total"],
                        "bytes": summary["bytes"],
                    }
                )
    return {
        "devices": result,
        "storage": {
            "history_bytes": sum(item["bytes"] for item in result),
            "recovery_bytes": _recovery_bytes(store),
        },
    }


def _recovery_bytes(store):
    if store is None:
        return 0
    total = 0
    for path in store.root.parent.glob(".ld2410-storage-*"):
        if path.is_dir() and not path.is_symlink():
            total += _directory_bytes(path)
    return total


def _directory_bytes(root):
    total = 0
    for folder, directories, files in os.walk(root, followlinks=False):
        directories[:] = [name for name in directories if not (Path(folder) / name).is_symlink()]
        for name in files:
            try:
                info = (Path(folder) / name).lstat()
                if stat.S_ISREG(info.st_mode):
                    total += info.st_size
            except FileNotFoundError:
                pass
    return total
