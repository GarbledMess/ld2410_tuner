"""One local-time overnight learning pass, without device writes."""

import asyncio
import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo


def settings(runtime):
    saved = runtime.data.get("learning_schedule", {})
    return {
        "enabled": saved.get("enabled", False),
        "time": saved.get("time", "03:00"),
        "timezone": getattr(getattr(runtime.hass, "config", None), "time_zone", "UTC"),
        "last_day": saved.get("last_day"),
        "running": bool(runtime._nightly_task and not runtime._nightly_task.done()),
    }


def configure(runtime, enabled, at):
    if (
        not isinstance(enabled, bool)
        or not isinstance(at, str)
        or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", at)
    ):
        raise ValueError("Choose an enabled state and a valid HH:MM time")
    saved = runtime.data.setdefault("learning_schedule", {})
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
    await runtime.store.async_save(runtime.data)
    for device_id, device in list(runtime.data["devices"].items()):
        if not settings(runtime)["enabled"]:
            break
        if device.get("entities"):
            await _learn_device(runtime, device_id, device, day)
    await runtime.store.async_save(runtime.data)


async def _learn_device(runtime, device_id, device, day):
    attempt = {
        "day": day,
        "started_at": datetime.now(UTC).timestamp(),
        "status": "running",
    }
    device["nightly_learning"] = attempt
    runtime._schedule_save()
    try:
        learned = await runtime.async_learn(device_id, source="automatic")
        attempt.update(status=learned["status"], result_id=learned["id"])
    except asyncio.CancelledError:
        attempt.update(status="interrupted", error="Learning interrupted by integration shutdown")
        raise
    except Exception as error:
        attempt.update(status="error", error=str(error))
    finally:
        attempt["finished_at"] = datetime.now(UTC).timestamp()
        runtime._schedule_save()


async def stop(runtime):
    if runtime.unsub_nightly:
        runtime.unsub_nightly()
    tasks = list(runtime._learning_jobs.values())
    if runtime._nightly_task:
        tasks.append(runtime._nightly_task)
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def restore(runtime):
    for device in runtime.data["devices"].values():
        attempt = device.get("nightly_learning", {})
        if attempt.get("status") == "running":
            attempt.update(status="interrupted", error="Home Assistant restarted during learning")
