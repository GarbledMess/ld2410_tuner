"""Global retention/size controls and the single guarded persistence path."""

import logging
import time
from copy import deepcopy

from . import budget, policy

_LOGGER = logging.getLogger(__name__)
GUARDED = (
    "history",
    "history_labels",
    "label_revision",
    "training_state",
    "training_label_start",
    "training_expires_at",
    "history_legacy_histograms",
    "history_legacy_since",
)


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
    runtime._flush_history_block(device_id)
    runtime.set_training_state(device_id, "unknown")
    device["recording_enabled"] = enabled
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
    for device_id in runtime._history_runtime:
        runtime._flush_history_block(device_id)
    return await _persist(
        runtime, clean=True, target=None if target_mib is None else int(target_mib * policy.MIB)
    )


async def async_save(runtime):
    return await _persist(runtime)


async def _persist(runtime, *, values=None, clean=False, target=None):
    async with runtime._storage_lock:
        runtime._storage_status["busy"] = True
        try:
            return await _prepare_and_save(runtime, values, clean, target)
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


async def _prepare_and_save(runtime, values, clean, target):
    for _attempt in range(3):
        original = deepcopy(runtime.data)
        candidate = original if values is None else {**original, "storage_settings": values}
        pending = {
            key: list(value.get("samples", [])) for key, value in runtime._history_runtime.items()
        }
        prepared, changes, report = await runtime.hass.async_add_executor_job(
            budget.prepare, candidate, pending, time.time(), clean, target
        )
        if not _unchanged(runtime, original, pending, changes):
            continue
        _accept(runtime, prepared, changes, values)
        await runtime.store.async_save(prepared)
        runtime._storage_status.pop("error", None)
        runtime._storage_status.pop("blocked", None)
        runtime._storage_status.update(report)
        if clean or report["removed_automatic"] or report["removed_human"]:
            runtime._storage_status["last_trim"] = report
        return report
    raise ValueError(
        "Recording or labels changed during trimming. No stale cleanup was applied; retry Trim now."
    )


def _unchanged(runtime, original, pending, changes):
    if runtime.data.get("storage_settings") != original.get("storage_settings"):
        return False
    for key in changes:
        device = runtime.data["devices"].get(key)
        before = original["devices"][key]
        if device is None or any(device.get(field) != before.get(field) for field in GUARDED):
            return False
        if runtime._history_runtime.get(key, {}).get("samples", []) != pending.get(key, []):
            return False
    return True


def _accept(runtime, prepared, changes, values):
    if values is not None:
        runtime.data["storage_settings"] = dict(values)
    for key, (updated, _stats) in changes.items():
        device, saved = runtime.data["devices"][key], prepared["devices"][key]
        device.update(updated)
        for field in ("history_cleanup", "label_revision", "last_learning"):
            if field in saved:
                device[field] = saved[field]
            else:
                device.pop(field, None)
    if changes or values is not None:
        runtime._history_cache.clear()
