"""Global retention/size controls and the single guarded persistence path."""

import logging
import time
from copy import deepcopy

from ..const import TRAINING_FIELDS
from ..rooms import groups, membership
from ..worker.supervisor import Busy, Closed
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
    if enabled and device.get("excluded"):
        raise ValueError("This radar is excluded; include it again first")
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
    if not _only_raises_ceiling(policy.settings(runtime.data), values):
        return await _panel_errors(_persist(runtime, values=values, clean=True))
    # A higher ceiling removes nothing, so it needs no cleanup to take effect. Keep it even
    # when the cleanup fails: that failure is often the very reason for raising it.
    async with runtime._storage_lock:
        runtime.data["storage_settings"] = dict(values)
        runtime._history_cache.clear()
        await runtime.store.async_save(budget.copy_data(runtime.data))
    try:
        return await _persist(runtime, values=values, clean=True)
    except (Busy, Closed):
        raise
    except Exception as error:
        raise ValueError(
            f"The {values['max_mib']:g} MiB ceiling was saved, but the cleanup could not "
            f"finish: {_reason(error) or 'unknown error'}"
        ) from error


def _only_raises_ceiling(current, values):
    return values["max_mib"] >= current["max_mib"] and all(
        values[key] == current[key] for key in values if key != "max_mib"
    )


async def trim(runtime, target_mib=None):
    maximum = policy.settings(runtime.data)["max_mib"]
    if target_mib is None:
        # Trimming to the ceiling itself leaves no room: the next recorded minutes would put
        # every save back over it. Leave headroom unless the owner names a target.
        target = budget.headroom(int(maximum * policy.MIB))
    elif type(target_mib) not in (int, float) or not 1 <= target_mib <= maximum:
        raise ValueError("Trim target must be between 1 MiB and the configured limit")
    else:
        target = int(target_mib * policy.MIB)
    return await _panel_errors(_persist(runtime, clean=True, target=target))


async def _panel_errors(work):
    """Storage actions report worker and disk failures as their reason, not "Unknown error"."""
    try:
        return await work
    except (Busy, Closed, ValueError):
        raise
    except Exception as error:
        raise ValueError(
            f"Storage cleanup could not finish: {_reason(error) or 'unknown error'}"
        ) from error


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
            await retry_failure(runtime, error)
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
            _clear_failure(runtime)
            runtime._storage_status.update(report)
            return report
        retry = runtime.data.get("maintenance_retry", {})
        if time.time() < retry.get("after", 0):
            raise budget.BudgetExceeded(
                f"{BACKOFF} after {_failure_cause(runtime, retry)}. Automatic cleanup retries "
                f"at {_local_time(retry['after'])}; Trim now retries immediately."
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
        versions = {
            (key, date): runtime._days.day_version(key, date)
            for key in original["devices"]
            for date in runtime._days.days(key)
        }
        candidate = original if values is None else {**original, "storage_settings": values}

        snapshot = _snapshot_days(runtime, candidate, clean, target, values)

        accept = _accept_worker(runtime, original, marks, versions, values, clean, persist)

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


def _clear_failure(runtime):
    for key in ("error", "blocked", "last_failure"):
        runtime._storage_status.pop(key, None)


def _record_maintenance(runtime, prepared, report, clean):
    if clean:
        for key, device in prepared["devices"].items():
            if key in runtime.data["devices"] and "history_cleanup" in device:
                runtime.data["devices"][key]["history_cleanup"] = deepcopy(
                    device.get("history_cleanup", {})
                )
    _clear_failure(runtime)
    runtime._storage_status.update(report)
    if clean or report["removed_automatic"] or report["removed_human"]:
        runtime._storage_status["last_trim"] = report


def _days_unchanged(runtime, original, marks, versions, changes, keys):
    """Whether the cleanup still applies: its settings, the days it rewrites, each radar's
    labels and the day being recorded are as it read them. Recording within the live day
    does not matter: cleanup never touches that day, and a radar is always recording."""
    if runtime.data.get("storage_settings") != original.get("storage_settings"):
        return False
    if any(
        runtime._days.day_version(key, date) != versions.get((key, date))
        for key, date, _day in changes
    ):
        return False
    return all(
        key in runtime.data["devices"]
        and _live_date(_observation_mark(runtime, key)) == _live_date(marks[key])
        and all(runtime.data["devices"][key].get(field) == before.get(field) for field in GUARDED)
        for key, before in original["devices"].items()
        if key in keys
    )


def _observation_mark(runtime, key):
    return runtime._days.mark(key), runtime._days.seen(key)


def _live_date(mark):
    state = mark[0][1]
    return None if state is None else state[0]


def _accept_days(runtime, prepared, changes, values, metadata_keys=(), current=None):
    """Install the cleanup's days and metadata. A batch (``current`` given) counts a radar's
    histograms only in its last batch (``metadata_keys``), from a copy taken when it started:
    they are installed for the radars whose histograms are still that copy (``current``), and
    counted again (``labels._rebuild_soon``, the keys returned) for those that recorded
    labelled time meanwhile. Earlier batches leave the histograms as they are."""
    if values is not None:
        runtime.data["storage_settings"] = dict(values)
    final = set(metadata_keys)
    rebuild = []
    for key in {key for key, _date, _day in changes} | final:
        device = runtime.data["devices"][key]
        device["label_revision"] = prepared["devices"][key].get("label_revision", 0)
        if not _accept_histograms(
            device, prepared["devices"][key], current is None or key in current
        ):
            rebuild += [key] if key in final else []
        device.pop("last_learning", None)
        runtime._label_counts.pop(key, None)
    if changes or values is not None:
        runtime._history_cache.clear()
    return rebuild


def _accept_histograms(device, prepared, counted):
    """Install the cleanup's histograms if they still count the radar's time (``counted``);
    else keep the radar's (the batch's copy would drop the labelled time recorded since)."""
    if counted:
        device["histograms"] = deepcopy(prepared.get("histograms", {}))
    else:
        prepared["histograms"] = deepcopy(device.get("histograms", {}))
    return counted


def _current_histograms(runtime, original, result):
    """A batch's radars whose histograms are still those it copied (None: not a batch)."""
    if "guard_keys" not in result:
        return None
    return {
        key
        for key in result.get("metadata_keys", ())
        if runtime.data["devices"][key].get("histograms")
        == original["devices"][key].get("histograms")
    }


def _rebuild_histograms(runtime, keys):
    from . import labels

    for key in keys:
        labels._rebuild_soon(runtime, key)


def _prune_indexes(root):
    summary_cache.prune(root, max_bytes=0, max_cell_bytes=0, preserve_counts=True)


async def _adopt_counts(runtime, prepared, counts, marks, touched):
    live_changed = {key for key in touched if _observation_mark(runtime, key) != marks[key]}
    for key, counted in counts.items():
        label_counts.adopt(runtime, key, counted)
        if key not in live_changed or key not in runtime._label_counts:
            continue
        refreshed = await label_counts.refresh(runtime, key, runtime.data["devices"][key])
        if refreshed is not None:
            raw, _index = refreshed
            histograms = label_counts.stored(raw)
            runtime.data["devices"][key]["histograms"] = histograms
            prepared["devices"][key]["histograms"] = deepcopy(histograms)


def _accept_worker(runtime, original, marks, versions, values, clean, persist):
    from ..worker import maintenance
    from ..worker.supervisor import owned_preparation

    async def accept(result, directory):
        prepared, changes, report, staged = await owned_preparation(
            runtime.hass.async_add_executor_job(maintenance.load, result, directory)
        )
        relabelled = _relabelled(original, prepared)
        guard_keys = set(result.get("guard_keys", ()))
        touched = {key for key, _date, _day in changes} | relabelled | guard_keys
        rebuild = []

        def valid():
            return _days_unchanged(runtime, original, marks, versions, changes, touched)

        def install():
            final = result.get("metadata_keys", ())
            current = _current_histograms(runtime, original, result)
            rebuild.extend(_accept_days(runtime, prepared, changes, values, final, current))
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
        await _adopt_counts(runtime, prepared, result.get("counts", {}), marks, touched)
        _rebuild_histograms(runtime, rebuild)
        report["used_bytes"] = await owned_preparation(
            runtime.hass.async_add_executor_job(_accepted_size, runtime._days, prepared)
        )
        runtime._storage_status["used_bytes"] = report["used_bytes"]
        _save_profile(runtime, result)
        if persist or "guard_keys" in result:
            await runtime.store.async_save(prepared)
        return report

    return accept


def _accept_progress(runtime, prepared, result, clean):
    # Maintenance finished: a later failure backs off from the start again.
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


async def retry_failure(runtime, error=None):
    """Persist the claim while the caller owns the storage lock."""
    if time.time() < runtime.data.get("maintenance_retry", {}).get("after", 0):
        return
    runtime.data["maintenance_retry"] = backoff(next_delay(runtime.data), error)
    await runtime.store.async_save(budget.copy_data(runtime.data))


FIRST_RETRY = 3600
LAST_RETRY = policy.DAY


def next_delay(data):
    """The wait before maintenance tries again: an hour, doubling with each failure in a row
    up to a day. Never longer: the daily cleanup must not skip days, and a failure is most
    often passing (a worker busy or stopped, a radar recording meanwhile)."""
    previous = data.get("maintenance_retry", {}).get("delay")
    return FIRST_RETRY if previous is None else min(LAST_RETRY, max(FIRST_RETRY, previous * 2))


BACKOFF = "Storage maintenance is in failure backoff"


def backoff(delay, error=None):
    """The persisted retry claim. It keeps the cause, so the message survives a restart."""
    claim = {"delay": delay, "after": time.time() + delay}
    reason = _reason(error)
    if reason:
        claim["reason"] = reason
    return claim


def _reason(error):
    """``error`` as a cause: never a backoff refusal itself, which would nest on every save."""
    if error is None or BACKOFF in str(error):
        return None
    text = str(error).strip() or type(error).__name__
    return text[:512]


def _failure_cause(runtime, retry):
    for cause in (
        retry.get("reason"),
        runtime._storage_status.get("last_failure"),
        runtime._storage_status.get("error"),
    ):
        if cause and BACKOFF not in cause:
            return cause[:512].rstrip(". ")
    return "an earlier failure (its details were not kept across a restart)"


def _local_time(timestamp):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(timestamp))


def _failed_status(runtime, error, values, target):
    """Only a limit the protected metadata cannot meet pauses recording (``blocked``). Any
    other failure, and a refusal while backing off, leaves recording running: the store is
    briefly over its ceiling, and the next cleanup trims it, instead of radars going
    unrecorded until a restart."""
    status = runtime._storage_status
    status["error"] = str(error)
    if BACKOFF in str(error):
        _LOGGER.debug("Storage maintenance deferred: %s", error)
        return
    status["last_failure"] = str(error)
    if isinstance(error, budget.LimitImpossible) and values is None and target is None:
        status["blocked"] = True
    _LOGGER.exception("Storage maintenance could not finish")


def _accepted_size(store, prepared):
    return store.size() + budget.storage_bytes(prepared)


def _save_profile(runtime, result):
    if "profile" in result:
        runtime._storage_status["maintenance_profile"] = result["profile"]
