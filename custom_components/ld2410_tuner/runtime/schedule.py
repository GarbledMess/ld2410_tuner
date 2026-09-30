"""One local-time overnight learning pass with optional application of improvements."""

import asyncio
import logging
import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from ..calibration import automatic
from ..calibration.service import LearningEvidenceChanged

_LOGGER = logging.getLogger(__name__)


def settings(runtime):
    saved = runtime.data.get("learning_schedule", {})
    return {
        "enabled": saved.get("enabled", False),
        "auto_apply": saved.get("auto_apply", True),
        "time": saved.get("time", "03:00"),
        "timezone": getattr(getattr(runtime.hass, "config", None), "time_zone", "UTC"),
        "last_day": saved.get("last_day"),
        "running": bool(runtime._nightly_task and not runtime._nightly_task.done()),
    }


def configure(runtime, enabled, at, auto_apply=None):
    if (
        not isinstance(enabled, bool)
        or not isinstance(at, str)
        or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", at)
    ):
        raise ValueError("Choose an enabled state and a valid HH:MM time")
    if auto_apply is not None and not isinstance(auto_apply, bool):
        raise ValueError("Choose whether to automatically apply better overnight results")
    saved = runtime.data.setdefault("learning_schedule", {})
    if auto_apply is not None:
        saved["auto_apply"] = auto_apply
    saved.update(enabled=enabled, time=at)
    runtime._schedule_save()
    return settings(runtime)


async def tick(runtime, now):
    config = settings(runtime)
    if not config["enabled"] or config["running"]:
        return
    local = now.astimezone(ZoneInfo(config["timezone"]))
    day = local.date().isoformat()
    if local.strftime("%H:%M") < config["time"] or config["last_day"] == day:
        return
    runtime.data.setdefault("learning_schedule", {})["last_day"] = day
    runtime._nightly_task = runtime.hass.async_create_task(_run(runtime, day))


async def _run(runtime, day):
    # Persist the claim before starting: restarts and repeated DST hours do not rerun it.
    await runtime.async_save()
    for device_id, device in list(runtime.data["devices"].items()):
        if not settings(runtime)["enabled"]:
            break
        if device.get("entities") and device.get("recording_enabled", True):
            await _learn_device(runtime, device_id, device, day)
    await runtime.async_save()


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
        attempt["automatic_apply"] = {}
        await automatic.run(runtime, device_id, learned, attempt["automatic_apply"])
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


async def stop(runtime):
    if runtime.unsub_nightly:
        runtime.unsub_nightly()
    tasks = list(runtime._learning_jobs.values()) + list(runtime._comparison_jobs.values())
    if runtime._nightly_task:
        tasks.append(runtime._nightly_task)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def restore(runtime):
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
    human = learned.get("training", {}).get("duration", {})
    automatic = learned.get("estimated_training", {}).get("duration", {})
    fields = ("presence_recall", "presence_recall_lower", "false_positive_percent")
    return {
        field: human.get(field) if human.get(field) is not None else automatic.get(field)
        for field in fields
    }
