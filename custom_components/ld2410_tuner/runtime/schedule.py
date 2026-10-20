"""One local-time overnight learning pass with optional application of improvements."""

import asyncio
import logging
import re
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from homeassistant.core import callback

from ..calibration import automatic
from ..calibration.service import LearningEvidenceChanged
from ..presence import rebuilds
from ..worker import priority

_LOGGER = logging.getLogger(__name__)
_DISPATCH_GRACE = timedelta(minutes=15)


def settings(runtime):
    saved = runtime.data.get("learning_schedule", {})
    return {
        "enabled": saved.get("enabled", False),
        "auto_apply": saved.get("auto_apply", True),
        "auto_apply_scope": saved.get("auto_apply_scope", "overnight"),
        "time": saved.get("time", "03:00"),
        "timezone": getattr(getattr(runtime.hass, "config", None), "time_zone", "UTC"),
        "last_day": saved.get("last_day"),
        "running": bool(runtime._nightly_task and not runtime._nightly_task.done()),
    }


def configure(runtime, enabled, at, auto_apply=None, auto_apply_scope=None):
    if (
        not isinstance(enabled, bool)
        or not isinstance(at, str)
        or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", at)
    ):
        raise ValueError("Choose an enabled state and a valid HH:MM time")
    if auto_apply is not None and not isinstance(auto_apply, bool):
        raise ValueError("Choose whether to automatically apply better results")
    if auto_apply_scope is not None and auto_apply_scope not in ("overnight", "all"):
        raise ValueError("Choose overnight only or every completed Learn for automatic Apply")
    saved = runtime.data.setdefault("learning_schedule", {})
    if auto_apply_scope is not None:
        saved["auto_apply_scope"] = auto_apply_scope
    if auto_apply is not None:
        saved["auto_apply"] = auto_apply
    saved.update(enabled=enabled, time=at)
    runtime._schedule_save()
    return settings(runtime)


@callback
def tick(runtime, now):
    """Once per local day at the schedule time: the learning pass (which refreshes stale
    references first) when enabled, otherwise a references-only refresh."""
    config = settings(runtime)
    day = _due_day(config, now)
    if day is None or config["running"]:
        return
    if config["enabled"]:
        _start_learning(runtime, config, day)
    else:
        _refresh_references(runtime, day)


def _due_day(config, now):
    """Learning starts near its scheduled time, never as an afternoon catch-up."""
    local = now.astimezone(ZoneInfo(config["timezone"]))
    if not config["enabled"]:
        # Reference maintenance remains a once-daily refresh after missed schedules.
        return local.date().isoformat() if local.strftime("%H:%M") >= config["time"] else None
    for day in (local, local - timedelta(days=1)):
        scheduled = _scheduled_instant(day, config["time"])
        elapsed = now.astimezone(UTC) - scheduled.astimezone(UTC)
        if timedelta(0) <= elapsed < _DISPATCH_GRACE:
            return day.date().isoformat()
    return None


def _scheduled_instant(local, at):
    """Use the first occurrence of a repeated time, or the first minute after a DST gap."""
    hour, minute = map(int, at.split(":"))
    requested = local.replace(hour=hour, minute=minute, second=0, microsecond=0, fold=0)
    normalized = requested.astimezone(UTC).astimezone(local.tzinfo)
    if normalized.replace(tzinfo=None) == requested.replace(tzinfo=None):
        return requested
    # Find the gap's end, rather than shifting e.g. missing 01:30 to 02:30.
    low, high = 0, int((normalized - requested).total_seconds() // 60)
    while low < high:
        middle = (low + high) // 2
        probe = requested + timedelta(minutes=middle)
        roundtrip = probe.astimezone(UTC).astimezone(local.tzinfo)
        if roundtrip.replace(tzinfo=None) == probe.replace(tzinfo=None):
            high = middle
        else:
            low = middle + 1
    return requested + timedelta(minutes=low)


def _start_learning(runtime, config, day):
    if config["last_day"] == day:
        return
    runtime.data.setdefault("learning_schedule", {})["last_day"] = day
    runtime._nightly_task = runtime.hass.async_create_task(_run(runtime, day))


def _refresh_references(runtime, day):
    """With learning off, stale references still rebuild once a day; claimed separately
    so enabling learning during the dispatch window can still start its own pass."""
    saved = runtime.data.setdefault("learning_schedule", {})
    if saved.get("references_day") == day:
        return
    saved["references_day"] = day
    runtime._schedule_save()
    rebuilds.queue(runtime, reason="daily")


@priority.background
async def _run(runtime, day):
    # Persist the claim before starting: restarts and repeated DST hours do not rerun it.
    await runtime.async_save()
    # Out-of-date references rebuild here, before learning, rather than at each edit.
    await rebuilds.refresh(runtime, reason="overnight")
    # Learning yields to discovery/removal; retain this run's starting membership.
    devices = list(runtime.data["devices"].items())
    for device_id, device, members in _plan(runtime, devices):
        if not settings(runtime)["enabled"]:
            break
        await _learn_device(runtime, device_id, device, day)
        _share_attempt(runtime, device["nightly_learning"], members)
    await runtime.async_save()


def _plan(runtime, devices):
    """``(device_id, device, members)``: each learning set once, through its first eligible
    member, then every radar in no learning set. ``members`` share the set's outcome."""
    from ..rooms import groups

    eligible = {
        key: device
        for key, device in devices
        if device.get("entities") and device.get("recording_enabled", True)
    }
    plan, covered = [], set()
    try:
        found = groups.current_sets(runtime)
    except Exception as error:
        # Learn every radar on its own; each records why it could not learn.
        _LOGGER.warning("Scheduled learning could not read learning sets: %s", error)
        found = {}
    for item in found.values():
        members = [key for key in item["device_ids"] if key in eligible]
        covered.update(item["device_ids"])
        if members:
            plan.append((members[0], eligible[members[0]], members))
    plan.extend((key, device, []) for key, device in eligible.items() if key not in covered)
    return plan


def _share_attempt(runtime, attempt, members):
    """Every member of a learning set records the set's one overnight outcome."""
    for key in members:
        device = runtime.data["devices"].get(key)
        if device is not None:
            device["nightly_learning"] = deepcopy(attempt)


async def _learn_device(runtime, device_id, device, day):
    attempt = {
        "day": day,
        "started_at": datetime.now(UTC).timestamp(),
        "status": "running",
    }
    device["nightly_learning"] = attempt
    runtime._schedule_save()
    try:
        learned = await _learn_fresh(runtime, device_id, attempt)
        attempt.update(
            result_id=learned["id"],
            timing=learned.get("timing"),
            assessment=_assessment(learned),
        )
        await _apply_result(runtime, device_id, device, learned, attempt)
        attempt["status"] = learned["status"]
    except asyncio.CancelledError:
        attempt.update(status="interrupted", error="Learning interrupted by integration shutdown")
        raise
    except Exception as error:
        attempt.update(status="error", error=str(error))
        _LOGGER.warning("Scheduled learning failed for device %s: %s", device_id, error)
    finally:
        attempt["finished_at"] = datetime.now(UTC).timestamp()
        runtime._schedule_save()


async def _apply_result(runtime, device_id, device, learned, attempt):
    job = device.get("learning_job", {})
    application = job.get("automatic_apply")
    if application and job.get("results", {}).get("automatic") == learned["id"]:
        # Manual and overnight callers can share a fit. Never apply that fit twice.
        attempt["automatic_apply"] = deepcopy(application)
        return
    attempt["automatic_apply"] = {}
    await automatic.run(runtime, device_id, learned, attempt["automatic_apply"])


async def stop(runtime):
    if runtime.unsub_nightly:
        runtime.unsub_nightly()
    tasks = (
        list(runtime._learning_jobs.values())
        + list(runtime._comparison_jobs.values())
        + list(runtime._room_jobs.values())
        + list(runtime._room_learning_jobs.values())
        + list(runtime._room_attribution_jobs.values())
    )
    if runtime._nightly_task:
        tasks.append(runtime._nightly_task)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def restore(runtime):
    _restore_joint(runtime)
    for device in runtime.data["devices"].values():
        recovery = device.get("configuration_recovery", {})
        if recovery.get("status") == "running":
            recovery.update(status="interrupted", error="Home Assistant restarted during recovery")
        for key in ("nightly_learning", "learning_job"):
            attempt = device.get(key, {})
            if attempt.get("status") == "running":
                _interrupt_attempt(attempt)


def _interrupt_attempt(attempt):
    application = attempt.get("automatic_apply", {})
    if application.get("status") in ("comparing", "applying"):
        application.update(
            status="interrupted", reason="Home Assistant restarted; check live thresholds."
        )
    attempt.update(
        status="interrupted",
        error="Home Assistant restarted during learning",
        finished_at=datetime.now(UTC).timestamp(),
    )


async def _learn_fresh(runtime, device_id, attempt):
    for number in (1, 2):
        attempt["attempts"] = number
        try:
            return await runtime.async_learn(device_id, source="automatic")
        except LearningEvidenceChanged:
            if number == 2:
                raise
            attempt["retry_reason"] = "Labels changed; retrying with the updated labels"
            runtime._schedule_save()


def _assessment(learned):
    """Keep the overnight headline tied to that run, including automatic-only fits."""
    if learned.get("joint"):
        return learned["joint"]["report"]["room"]
    human = learned.get("training", {}).get("duration", {})
    automatic = learned.get("estimated_training", {}).get("duration", {})
    fields = ("presence_recall", "presence_recall_lower", "false_positive_percent")
    return {
        field: human.get(field) if human.get(field) is not None else automatic.get(field)
        for field in fields
    }


def _restore_joint(runtime):
    for state in runtime.data.get("room_learning", {}).values():
        job = state.get("job", {})
        if job.get("status") == "running":
            _interrupt_attempt(job)
        application = state.get("application", {})
        if application.get("status") == "applying":
            application.update(
                status="interrupted",
                reason="Home Assistant restarted; check every zone member's thresholds",
            )
