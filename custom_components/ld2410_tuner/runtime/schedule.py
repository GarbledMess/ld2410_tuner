"""One local-time overnight learning pass with optional application of improvements."""

import asyncio
import logging
import re
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfoNotFoundError

from homeassistant.core import callback
from homeassistant.util import dt as dt_util

from ..calibration import automatic
from ..calibration.service import LearningEvidenceChanged
from ..history import storage
from ..presence import rebuilds
from ..worker import priority
from . import overnight

_LOGGER = logging.getLogger(__name__)
_DISPATCH_GRACE = timedelta(minutes=15)
# After a restart the radars need time to come back, or the next Learn only records that.
_RESUME_SETTLE = timedelta(minutes=5)
# An item is started again once after a restart; one that keeps dying must not loop.
_MAX_STARTS = 2


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
    if config["running"] or _resume(runtime, config, now):
        return
    day = _due_day(config, now)
    if day is None:
        return
    if config["enabled"]:
        _start_learning(runtime, config, day)
    else:
        _refresh_references(runtime, day)


def _resume(runtime, config, now):
    """Continue a night a restart cut short, the same local day; True while one is held.
    ``last_day`` is already claimed, so this is the only way that day's items run again."""
    saved = runtime.data.get("learning_schedule", {})
    queue = saved.get("queue")
    if not queue:
        return False
    if not config["enabled"] or queue["day"] != _local(config, now).date().isoformat():
        saved.pop("queue")
        runtime._nightly_resume_at = None
        runtime._schedule_save()
        return False
    if runtime._nightly_resume_at is None:
        runtime._nightly_resume_at = now + _RESUME_SETTLE
    if now >= runtime._nightly_resume_at:
        runtime._nightly_resume_at = None
        _launch(runtime, queue["day"])
    return True


def _local(config, now):
    zone = dt_util.get_time_zone(config["timezone"])
    if zone is None:
        raise ZoneInfoNotFoundError(f"No time zone found with key {config['timezone']}")
    return now.astimezone(zone)


def _due_day(config, now):
    """Learning starts near its scheduled time, never as an afternoon catch-up."""
    local = _local(config, now)
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
    _launch(runtime, day)


def _launch(runtime, day):
    runtime._nightly_task = runtime.hass.async_create_background_task(
        _run(runtime, day), "ld2410_tuner overnight learning"
    )


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
    saved = runtime.data.setdefault("learning_schedule", {})
    # Learning yields to discovery/removal; retain this run's starting membership.
    plan = _queued(runtime, saved, list(runtime.data["devices"].items()), day)
    try:
        # Persist the claim and queue first: restarts and repeated DST hours do not rerun it.
        await runtime.async_save()
        # Out-of-date references rebuild here, before learning, rather than at each edit.
        await rebuilds.refresh(runtime, reason="overnight")
        for item in plan:
            if not settings(runtime)["enabled"]:
                break
            await _advance(runtime, saved["queue"], item, day)
        saved.pop("queue", None)
        await runtime.async_save()
    except Exception:
        # Only a shutdown leaves a queue: a pass that failed must not restart every minute.
        saved.pop("queue", None)
        raise


def _queued(runtime, saved, devices, day):
    """Tonight's items in order: what is left of a saved queue, otherwise a new one."""
    plan = _plan(runtime, devices)
    queue = saved.get("queue")
    if queue and queue.get("day") == day:
        return _still_queued(plan, queue["items"])
    plan.sort(key=lambda item: overnight.priority(item, overnight.read(runtime, item)))
    saved["queue"] = {"day": day, "items": [item.lead for item in plan], "started": {}}
    return plan


def _still_queued(plan, leads):
    """Membership may have changed over the restart: a set stays queued through any member."""
    position = {key: index for index, key in enumerate(leads)}
    found = {}
    for item in plan:
        places = [position[key] for key in (item.lead, *item.members) if key in position]
        if places:
            found[min(places)] = item
    return [found[place] for place in sorted(found)]


async def _advance(runtime, queue, item, day):
    await _yield_to_user_learning(runtime)
    before = overnight.read(runtime, item)
    if overnight.unchanged(runtime, item, before):
        _record_unchanged(runtime, item, day)
    elif _claim(queue, item):
        await _checkpoint(runtime)  # The start counts even if this Learn stops Home Assistant.
        await _learn_device(runtime, item.lead, item.device, day)
        _share_attempt(runtime, item.device["nightly_learning"], item.members)
        overnight.remember(runtime, item, before, day)
    queue["items"] = [key for key in queue["items"] if key not in (item.lead, *item.members)]
    await _checkpoint(runtime)


async def _yield_to_user_learning(runtime):
    """The worker already prefers a user's fit, but one still preparing has not queued yet."""
    while waiting := _user_learning(runtime):
        await asyncio.wait(waiting)


def _user_learning(runtime):
    devices = runtime.data["devices"]
    return {
        task
        for key, task in runtime._learning_jobs.items()
        if not task.done()
        and "user" in devices.get(key, {}).get("learning_job", {}).get("sources", ())
    }


def _claim(queue, item):
    starts = queue["started"].get(item.lead, 0)
    if starts >= _MAX_STARTS:
        _LOGGER.warning("Scheduled learning gave up on %s after repeated restarts", item.lead)
        return False
    queue["started"][item.lead] = starts + 1
    return True


async def _checkpoint(runtime):
    """Between items, without the cleanup a full save may run; a failure costs at most a repeat."""
    try:
        await storage.save_directly(runtime)
    except Exception as error:
        _LOGGER.warning("Scheduled learning could not save its progress: %s", error)


def _record_unchanged(runtime, item, day):
    """Nothing to learn from: keep the result and say so; nothing is applied."""
    stamp = datetime.now(UTC).timestamp()
    learned_day = item.device[overnight.BASELINE]["day"]
    for key in item.members or [item.lead]:
        device = runtime.data["devices"][key]
        kept = device["nightly_learning"]
        device["nightly_learning"] = {
            "day": day,
            "status": "unchanged",
            "started_at": stamp,
            "finished_at": stamp,
            "learned_day": learned_day,
            "result_id": kept["result_id"],
            "timing": kept.get("timing"),
            "assessment": kept.get("assessment"),
        }
    runtime._schedule_save()


def _plan(runtime, devices):
    """Each learning set once, through its first eligible member, then every radar in no
    learning set. An item's ``members`` share the set's outcome."""
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
            lead = members[0]
            plan.append(overnight.Item(lead, eligible[lead], members, item["group_ids"]))
    plan.extend(
        overnight.Item(key, device, [], [])
        for key, device in eligible.items()
        if key not in covered
    )
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
