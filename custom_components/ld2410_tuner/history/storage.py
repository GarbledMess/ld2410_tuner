"""Global retention/size controls and the single guarded persistence path."""

import logging
import time
from copy import deepcopy

from ..const import TRAINING_FIELDS
from ..rooms import groups, membership
from . import budget, commit, ingest, label_counts, pace, policy, summary_cache

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
    auto["open_segment"] = None
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


async def save_directly(runtime):
    """Labels and settings need no worker; only the day files' cleanup does, and that can wait."""
    async with runtime._storage_lock:
        await runtime.store.async_save(budget.copy_data(runtime.data))


async def _persist(runtime, *, values=None, clean=False, target=None, persist=True):
    from . import live

    live.settle(runtime)  # Saved references include the evidence waiting for its batch.
    async with runtime._storage_lock:
        if runtime._days is not None:
            from ..worker import flushing

            await flushing.flush(runtime, locked=True)
        runtime._storage_status["busy"] = True
        try:
            return await _prepare_and_save(runtime, values, clean, target, persist)
        except Exception as error:
            await _record_failure(runtime, error, clean, values, target)
            raise
        finally:
            runtime._storage_status["busy"] = False


async def _record_failure(runtime, error, clean, values, target):
    if not clean and values is None and target is None:
        try:
            await retry_failure(runtime)
        except Exception as retry_error:
            try:
                error.add_note(f"Retry bookkeeping also failed: {retry_error}")
            except MemoryError:
                pass
    _failed_status(runtime, error, values, target)


async def _prepare_and_save(runtime, values, clean, target, persist=True):
    if not clean and values is None and target is None:
        candidate = budget.copy_data(runtime.data)
        report = await runtime.hass.async_add_executor_job(
            _routine_report, runtime._days, candidate
        )
        if report is not None:
            if persist:
                await runtime.store.async_save(candidate)
            runtime._storage_status.pop("error", None)
            runtime._storage_status.pop("blocked", None)
            runtime._storage_status.update(report)
            return report
        if time.time() < runtime.data.get("maintenance_retry", {}).get("after", 0):
            previous = runtime._storage_status.get("last_failure") or runtime._storage_status.get(
                "error", "unknown failure"
            )
            raise budget.BudgetExceeded(
                f"Storage maintenance is in failure backoff after {previous[:512]}; "
                "Trim retries now"
            )
    return await _save_days(runtime, values, clean, target, persist)


def _routine_report(store, candidate):
    from ..worker import storage_values

    storage_values.validate(candidate)
    used = (0 if store is None else store.size()) + budget.storage_bytes(candidate)
    ceiling = int(policy.settings(candidate)["max_mib"] * policy.MIB)
    if used <= ceiling:
        return budget._report(used, used, ceiling, time.time(), 0.0, 0.0)
    return None


@pace.background
def _prepare_day_save(store, candidate, now, clean, target):
    """A save's day replacements (executor); a cleanup is low-priority background work."""
    prepare = _prepare_cleanup if clean else _prepare_save
    return prepare(store, candidate, now, clean, target)


@pace.background
def _prepare_cleanup(store, candidate, now, clean, target):
    return _prepare_save(store, candidate, now, clean, target)


def _prepare_save(store, candidate, now, clean, target):
    ceiling = int(policy.settings(candidate)["max_mib"] * policy.MIB)
    limit = ceiling if target is None else min(ceiling, target)
    json_bytes = budget.storage_bytes(candidate)  # Measured once per save: it is the costly part.
    if store.size() + json_bytes > limit:
        summary_cache.prune(store.root, max_bytes=0, max_cell_bytes=0, preserve_counts=True)
        if (
            summary_cache.disk_bytes(store.root, preserve_counts=True)
            and store.size() + json_bytes > limit
        ):
            raise budget.BudgetExceeded(
                "Optional history indexes could not be removed. Retry when their files are "
                "available; recorded history was not trimmed to make room for the indexes."
            )
    encoded = {}
    prepared, changes, report = budget.prepare_days(
        store, candidate, now, clean, target, before=store.size() + json_bytes, encoded=encoded
    )
    return prepared, changes, report, store.stage(changes, encoded)


def _batched(store, candidate):
    if candidate.get("maintenance_progress"):
        return True
    size = store.size()
    ceiling = int(policy.settings(candidate)["max_mib"] * policy.MIB)
    return (
        8 * policy.MIB <= size <= ceiling - budget.storage_bytes(candidate)
        and sum(len(store.days(key)) for key in candidate["devices"]) > 4
    )


def _snapshot_days(runtime, candidate, clean, target, values):
    from ..worker import retention

    def snapshot(directory):
        extra = {"clean": clean, "target": target, "live": _live_metadata(runtime._days)}
        if clean and values is None and target is None and _batched(runtime._days, candidate):
            extra.update(retention.snapshot(runtime._days, candidate, time.time()))
        return extra

    return snapshot


async def _save_days(runtime, values, clean, target, persist=True):
    from ..worker import transactions

    for _attempt in range(3):
        # Bounded; the copy is validated in the executor, before anything is copied for it.
        original = budget.copy_data(runtime.data, max_nodes=transactions.MESSAGE_NODES)
        marks = {key: _observation_mark(runtime, key) for key in original["devices"]}
        candidate = original if values is None else {**original, "storage_settings": values}

        snapshot = _snapshot_days(runtime, candidate, clean, target, values)

        accept = _accept_worker(runtime, original, marks, values, clean, persist)

        report = await transactions.run(
            runtime, "maintenance", candidate, accept, snapshot=snapshot
        )
        if report is not None:
            return report
    raise ValueError(
        "Recording or labels changed during trimming. No stale cleanup was applied; retry Trim now."
    )


def _live_metadata(store):
    with store._lock:
        return {
            key: {
                "date": str(live.date),
                "last": live.last,
                "seen": live.seen,
                "current": None if live.builder.current is None else list(live.builder.current),
                "run": None if live.builder.run is None else list(live.builder.run),
            }
            for key, live in store._live.items()
        }


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
            if key in runtime.data["devices"] and "history_cleanup" in device:
                runtime.data["devices"][key]["history_cleanup"] = deepcopy(
                    device.get("history_cleanup", {})
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


def _accept_days(runtime, prepared, changes, values, metadata_keys=()):
    if values is not None:
        runtime.data["storage_settings"] = dict(values)
    for key in {key for key, _date, _day in changes} | set(metadata_keys):
        device = runtime.data["devices"][key]
        device["label_revision"] = prepared["devices"][key].get("label_revision", 0)
        device["histograms"] = deepcopy(prepared["devices"][key].get("histograms", {}))
        device.pop("last_learning", None)
        runtime._label_counts.pop(key, None)
    if changes or values is not None:
        runtime._history_cache.clear()


def _prune_indexes(root):
    summary_cache.prune(root, max_bytes=0, max_cell_bytes=0, preserve_counts=True)


def _accept_worker(runtime, original, marks, values, clean, persist):
    from ..worker import maintenance
    from ..worker.supervisor import owned_preparation

    async def accept(result, directory):
        prepared, changes, report, staged = await owned_preparation(
            runtime.hass.async_add_executor_job(maintenance.load, result, directory)
        )
        relabelled = _relabelled(original, prepared)
        touched = (
            {key for key, _date, _day in changes} | relabelled | set(result.get("guard_keys", ()))
        )

        def valid():
            return _days_unchanged(runtime, original, marks, touched)

        def install():
            _accept_days(runtime, prepared, changes, values, result.get("metadata_keys", ()))
            _accept_labels(runtime, prepared, relabelled)
            _record_maintenance(runtime, prepared, report, clean)
            _accept_progress(runtime, prepared, result, clean)

        if result.get("prune"):
            await owned_preparation(
                runtime.hass.async_add_executor_job(_prune_indexes, runtime._days.root)
            )
        if not await commit.publish(runtime, staged, valid, install):
            return None
        from . import maintenance_cache

        await owned_preparation(
            runtime.hass.async_add_executor_job(
                maintenance_cache.transfer, directory / "history", runtime._days.root
            )
        )
        report["used_bytes"] = await owned_preparation(
            runtime.hass.async_add_executor_job(_accepted_size, runtime._days, prepared)
        )
        runtime._storage_status["used_bytes"] = report["used_bytes"]
        for key, counted in result.get("counts", {}).items():
            label_counts.adopt(runtime, key, counted)  # Published: the days it counted.
        _save_profile(runtime, result)
        if persist or "guard_keys" in result:
            await runtime.store.async_save(prepared)
        return report

    return accept


def _accept_progress(runtime, prepared, result, clean):
    if clean:
        runtime.data.pop("maintenance_retry", None)
        prepared.pop("maintenance_retry", None)
    if "guard_keys" not in result:
        if clean:
            runtime.data.pop("maintenance_progress", None)
            prepared.pop("maintenance_progress", None)
        return
    progress = prepared.get("maintenance_progress")
    if progress is None:
        runtime.data.pop("maintenance_progress", None)
    else:
        runtime.data["maintenance_progress"] = deepcopy(progress)


async def retry_failure(runtime):
    """Persist the claim while the caller owns the storage lock."""
    saved = runtime.data.get("maintenance_retry", {})
    if time.time() < saved.get("after", 0):
        return
    delay = min(7 * policy.DAY, saved.get("delay", policy.DAY // 2) * 2)
    runtime.data["maintenance_retry"] = {"delay": delay, "after": time.time() + delay}
    await runtime.store.async_save(budget.copy_data(runtime.data))


def _failed_status(runtime, error, values, target):
    if "failure backoff" not in str(error):
        runtime._storage_status["last_failure"] = str(error)
    runtime._storage_status.update(
        error=str(error),
        blocked=isinstance(error, budget.BudgetExceeded) and values is None and target is None,
    )
    _LOGGER.exception("Storage maintenance could not finish")


def _accepted_size(store, prepared):
    return store.size() + budget.storage_bytes(prepared)


def _save_profile(runtime, result):
    if "profile" in result:
        runtime._storage_status["maintenance_profile"] = result["profile"]
