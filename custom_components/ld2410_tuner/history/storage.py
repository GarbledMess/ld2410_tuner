"""Global retention/size controls and the single guarded persistence path."""

import logging
import time
from copy import deepcopy

from ..const import TRAINING_FIELDS
from ..rooms import groups, membership
from . import budget, ingest, policy, summary_cache

_LOGGER = logging.getLogger(__name__)
GUARDED = ("history_labels", "label_revision", *TRAINING_FIELDS)


def summary(runtime):
    return {"settings": policy.settings(runtime.data), **runtime._storage_status}


def set_recording(runtime, device_id, enabled):
    if type(enabled) is not bool:
        raise ValueError("Recording must be enabled or disabled")
    device = runtime.data["devices"].get(device_id)
    if device is None:
        raise ValueError("Unknown device")
    if device.get("recording_enabled", True) == enabled:
        return {"enabled": enabled}
    if not enabled:
        ingest.end(runtime, device_id, time.time())
    runtime.set_training_state(device_id, "unknown")
    before = groups.definitions(runtime)[2]
    device["recording_enabled"] = enabled
    # Area groups list only recording radars, so pausing can change room membership.
    membership.membership_changed(runtime, before)
    runtime._auto_runtime.pop(device_id, None)
    runtime._source_runtime.pop(device_id, None)
    auto = device.get("auto", {})
    auto.pop("last_classification", None)
    auto.pop("filter", None)
    if auto.get("segments") and auto["segments"][-1].get("end") is None:
        auto["segments"][-1]["end"] = time.time()
    runtime._schedule_save()
    return {"enabled": enabled}


async def configure(runtime, values):
    values = policy.validate(values)
    return await _persist(runtime, values=values, clean=True)


async def trim(runtime, target_mib=None):
    maximum = policy.settings(runtime.data)["max_mib"]
    if target_mib is not None:
        if type(target_mib) not in (int, float) or not 1 <= target_mib <= maximum:
            raise ValueError("Trim target must be between 1 MiB and the configured limit")
    return await _persist(
        runtime, clean=True, target=None if target_mib is None else int(target_mib * policy.MIB)
    )


async def async_save(runtime):
    return await _persist(runtime)


async def _persist(runtime, *, values=None, clean=False, target=None, persist=True):
    from . import live

    live.settle(runtime)  # Saved references include the evidence waiting for its batch.
    async with runtime._storage_lock:
        if runtime._days is not None:
            await runtime.hass.async_add_executor_job(runtime._days.flush)
        runtime._storage_status["busy"] = True
        try:
            return await _prepare_and_save(runtime, values, clean, target, persist)
        except ValueError as error:
            runtime._storage_status.update(
                error=str(error),
                blocked=isinstance(error, budget.BudgetExceeded)
                and values is None
                and target is None,
            )
            _LOGGER.warning("Storage maintenance could not finish: %s", error)
            raise
        finally:
            runtime._storage_status["busy"] = False


async def _prepare_and_save(runtime, values, clean, target, persist=True):
    return await _save_days(runtime, values, clean, target, persist)


def _prepare_day_save(store, candidate, now, clean, target):
    ceiling = int(policy.settings(candidate)["max_mib"] * policy.MIB)
    limit = ceiling if target is None else min(ceiling, target)
    json_bytes = budget.storage_bytes(candidate)  # Measured once per save: it is the costly part.
    if store.size() + json_bytes > limit:
        summary_cache.prune(store.root, max_bytes=0)
        if summary_cache.disk_bytes(store.root) and store.size() + json_bytes > limit:
            raise budget.BudgetExceeded(
                "Optional history indexes could not be removed. Retry when their files are "
                "available; recorded history was not trimmed to make room for the indexes."
            )
    prepared, changes, report = budget.prepare_days(
        store, candidate, now, clean, target, before=store.size() + json_bytes
    )
    return prepared, changes, report, store.stage(changes)


async def _save_days(runtime, values, clean, target, persist=True):
    for _attempt in range(3):
        original = budget.copy_data(runtime.data)
        marks = {key: _observation_mark(runtime, key) for key in original["devices"]}
        candidate = original if values is None else {**original, "storage_settings": values}
        prepared, changes, report, staged = await runtime.hass.async_add_executor_job(
            _prepare_day_save, runtime._days, candidate, time.time(), clean, target
        )
        relabelled = _relabelled(original, prepared)
        touched = {key for key, _date, _day in changes} | relabelled
        if not _days_unchanged(runtime, original, marks, touched):
            runtime._days.discard(staged)
            continue
        runtime._days.commit(staged)
        _accept_days(runtime, prepared, changes, values)
        _accept_labels(runtime, prepared, relabelled)
        _record_maintenance(runtime, prepared, report, clean)
        if persist:
            await runtime.store.async_save(prepared)
        return report
    raise ValueError(
        "Recording or labels changed during trimming. No stale cleanup was applied; retry Trim now."
    )


def _relabelled(original, prepared):
    """Radars whose labels the cleanup expired."""
    return {
        key
        for key, device in prepared["devices"].items()
        if device.get("history_labels") != original["devices"].get(key, {}).get("history_labels")
    }


def _accept_labels(runtime, prepared, keys):
    """Install expired labels: a new revision, so an edit of the old list is refused."""
    for key in keys:
        device = runtime.data["devices"][key]
        device["history_labels"] = deepcopy(prepared["devices"][key]["history_labels"])
        device["label_revision"] = device.get("label_revision", 0) + 1
        prepared["devices"][key]["label_revision"] = device["label_revision"]
        runtime._label_counts.pop(key, None)


def _record_maintenance(runtime, prepared, report, clean):
    if clean:
        for key, device in prepared["devices"].items():
            if key in runtime.data["devices"]:
                runtime.data["devices"][key]["history_cleanup"] = deepcopy(
                    device["history_cleanup"]
                )
    runtime._storage_status.pop("error", None)
    runtime._storage_status.pop("blocked", None)
    runtime._storage_status.update(report)
    if clean or report["removed_automatic"] or report["removed_human"]:
        runtime._storage_status["last_trim"] = report


def _days_unchanged(runtime, original, marks, keys):
    if runtime.data.get("storage_settings") != original.get("storage_settings"):
        return False
    return all(
        key in runtime.data["devices"]
        and _observation_mark(runtime, key) == marks[key]
        and all(runtime.data["devices"][key].get(field) == before.get(field) for field in GUARDED)
        for key, before in original["devices"].items()
        if key in keys
    )


def _observation_mark(runtime, key):
    return runtime._days.mark(key), runtime._days.seen(key)


def _accept_days(runtime, prepared, changes, values):
    if values is not None:
        runtime.data["storage_settings"] = dict(values)
    for key in {key for key, _date, _day in changes}:
        device = runtime.data["devices"][key]
        device["label_revision"] = prepared["devices"][key]["label_revision"]
        device["histograms"] = deepcopy(prepared["devices"][key]["histograms"])
        device.pop("last_learning", None)
        runtime._label_counts.pop(key, None)
    if changes or values is not None:
        runtime._history_cache.clear()
