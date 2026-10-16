"""Recording operations on the shared runtime state."""

from __future__ import annotations

import asyncio
import math
import time
from copy import deepcopy

from homeassistant.core import callback

from ..const import (
    AUTO_SAMPLE_INTERVAL,
    TRAINING_FIELDS,
)
from . import guesses, label_counts, live
from .policy import retention_seconds, settings


def _record_history_sample(runtime, device_id: str, values: dict[str, float], now: float) -> None:
    """The poll records ``values`` read at ``now`` and the radar's guess (``live.poll``)."""
    device = _recording_device(runtime, device_id)
    if device is None:
        return
    live.poll(runtime, device_id, device, values, now, _current_guess(device, now))


def _recording_device(runtime, device_id):
    """The radar currently allowed to record."""
    device = runtime.data.get("devices", {}).get(device_id)
    if device is None or not device.get("recording_enabled", True):
        return None
    return device


@callback
def _enforce_history_retention(runtime, *_args) -> None:
    if runtime._cleanup_task is None or runtime._cleanup_task.done():
        runtime._cleanup_task = runtime.hass.async_create_background_task(
            runtime.async_clean_history(), "ld2410_tuner history cleanup"
        )


async def async_clean_history(runtime, *, persist=True):
    """Normalize stored history at startup and hourly, without racing writes."""
    from . import storage

    report = await storage._persist(runtime, clean=True, persist=persist)
    for device_id in list(runtime.data["devices"]):
        await label_counts.ensure(runtime, device_id)
    return bool(report["removed_automatic_seconds"] or report["removed_human_seconds"])


async def flush_history(runtime) -> None:
    runtime._schedule_save()
    if runtime._save_task:
        await asyncio.shield(runtime._save_task)


def history_mark(runtime, device_id):
    """What changes whenever ``device_id``'s recorded history does (``DayStore.mark``)."""
    days = getattr(runtime, "_days", None)
    return None if days is None else days.mark(device_id)


def has_history(runtime, device_id):
    """Whether anything is recorded for ``device_id``."""
    days = getattr(runtime, "_days", None)
    return bool(days is not None and days.days(device_id))


def newest(runtime, device_id, since):
    """The end of ``device_id``'s newest recorded observation at or after ``since``: the view's
    time (now) while its coverage is open, else when it ended; None when there is none."""
    days = getattr(runtime, "_days", None)
    if days is None:
        return None
    last = days.last(device_id)
    until = getattr(runtime, "_history_until", None)
    if days.current(device_id) is not None:  # As the store reads it: a view at its own time,
        return until if until is not None else max(time.time(), days.seen(device_id))
    return last if last is not None and last >= since else None


def _device_id(runtime, device):
    return next(
        (key for key, candidate in runtime.data["devices"].items() if candidate is device), None
    )


def history_summary(runtime, device_id):
    """``{"days", "oldest_day", "newest"}`` of a radar's recorded history for the snapshot:
    how many days hold recordings (the folder listing; no file is read), the first of them
    and the end of the newest observation."""
    days = getattr(runtime, "_days", None)
    dates = days.days(device_id) if days is not None else []
    return {
        "days": len(dates),
        "oldest_day": dates[0].isoformat() if dates else None,
        "newest": newest(runtime, device_id, -math.inf),
    }


def _history_view(runtime, device_id, members=True):
    from ..rooms import attribution
    from ..runtime.coordinator import TunerRuntime

    device = runtime.data["devices"].get(device_id)
    if not device:
        raise ValueError("Unknown device")
    snapshot = {key: device.get(key) for key in TRAINING_FIELDS}
    snapshot.update(_histograms_copy(device))
    snapshot["history_labels"] = deepcopy(device.get("history_labels", []))
    view = TunerRuntime(
        None,
        None,
        {
            "devices": {device_id: snapshot},
            "storage_settings": settings(runtime.data),
            # Learning settings (the automatic evidence weight) travel to executor fits.
            "learning_schedule": deepcopy(runtime.data.get("learning_schedule", {})),
        },
    )
    view._days, view._history_until = runtime._days, time.time()
    view._snapshot_history_start = max(0, time.time() - retention_seconds(runtime.data))
    # Executor jobs cannot read the registries, so room attribution inputs travel with the view.
    saved = view.data["room_snapshot"] = attribution.snapshot(runtime, device_id)
    # Room estimates read the other members' recorded guesses, so their history travels too.
    for key in (saved or {}).get("members", {}) if members else ():
        other = runtime.data["devices"].get(key)
        if key != device_id and other is not None:
            view.data["devices"][key] = _histograms_copy(other)
    # What it read stays owned while no recorded history is rewritten (``ownership``).
    days = runtime._days
    view._history_rewrites = {
        key: days.rewrites(key) for key in view.data["devices"] if days is not None
    }
    return view


def _histograms_copy(device):
    """A view's copy of a radar's labelled histograms, which recording keeps counting into
    (they set the radar's quiet baseline, ``trust.ceilings``)."""
    return {"histograms": deepcopy(device.get("histograms", {}))}


def _current_guess(device, now):
    """``(state, percent, basis)`` of the radar's guess for a sample taken ``now``."""
    last = device.get("auto", {}).get("last_classification", {})
    basis = guesses.EXTERNAL if last.get("basis") == "external" else guesses.REFERENCE
    if last.get("buffered"):
        return "unknown", 0, basis  # Confirm only after the external end buffer has elapsed.
    state = (
        last.get("state")
        if now - last.get("timestamp", 0) <= AUTO_SAMPLE_INTERVAL * 2
        else "unknown"
    )
    return state, max(0, min(100, round(last.get("confidence", 0) * 100))), basis
