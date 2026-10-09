"""Keep discovered radars reporting gate energies without repeated rapid writes."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta

from homeassistant.core import callback
from homeassistant.helpers.event import async_track_time_interval

from ..calibration import device_io, recovery

_LOGGER = logging.getLogger(__name__)
CHECK_INTERVAL = timedelta(minutes=10)
RETRY_DELAYS = (600, 1200, 1800)


def start(runtime):
    """Check on startup and periodically, including devices discovered later."""
    # Unmarked sync callbacks run in an executor thread, where creating the task is unsafe.
    runtime.unsub_engineering = async_track_time_interval(
        runtime.hass, callback(lambda now: tick(runtime)), CHECK_INTERVAL
    )
    tick(runtime)


@callback
def tick(runtime):
    if runtime._engineering_task and not runtime._engineering_task.done():
        return
    runtime._engineering_task = runtime.hass.async_create_task(_check_devices(runtime))


async def stop(runtime):
    if runtime.unsub_engineering:
        runtime.unsub_engineering()
        runtime.unsub_engineering = None
    if runtime._engineering_task:
        runtime._engineering_task.cancel()
        await asyncio.gather(runtime._engineering_task, return_exceptions=True)


def _switch(runtime, device_id, device):
    if not device.get("entities") or not device.get("recording_enabled", True):
        return None
    return recovery.capability(runtime, device_id, "switch", ("engineering_mode",))


def _recovering(runtime, device_id):
    sensor = recovery.capability(runtime, device_id, "sensor", ("ld2410_recovery_status",))
    state = runtime.hass.states.get(sensor) if sensor else None
    return state is not None and state.state in (
        "Waiting for settings",
        "Recovering missing settings",
    )


async def _check_devices(runtime):
    # Devices can change while a switch service call is awaiting completion.
    device_ids = list(runtime.data["devices"])
    for device_id in device_ids:
        device = runtime.data["devices"].get(device_id, {})
        entity_id = _switch(runtime, device_id, device)
        state = recovery._switch_state(runtime, entity_id)
        if state != "off":
            runtime._engineering_status.pop(device_id, None)
            if state == "on" and time.monotonic() >= runtime._engineering_attempts.get(
                device_id, {}
            ).get("retry_at", 0):
                runtime._engineering_attempts.pop(device_id, None)
            continue
        await _check_off(runtime, device_id, entity_id)


async def _check_off(runtime, device_id, entity_id):
    previous = runtime._engineering_attempts.get(device_id, {})
    if previous.get("entity_id") == entity_id and time.monotonic() < previous.get("retry_at", 0):
        runtime._engineering_status.setdefault(
            device_id,
            {
                "status": "waiting",
                "message": "Engineering Mode is off; waiting before retrying the enable command.",
            },
        )
        return
    if device_id in runtime._applying or _recovering(runtime, device_id):
        runtime._engineering_status[device_id] = {
            "status": "waiting",
            "message": "Engineering Mode is off; waiting for radar configuration to finish.",
        }
        return
    await _enable(runtime, device_id, entity_id, previous)


async def _enable(runtime, device_id, entity_id, previous):
    attempted_at = time.monotonic()
    report = {"status": "running", "message": "Enabling Engineering Mode for gate readings…"}
    runtime._engineering_status[device_id] = report
    try:
        with device_io.device_operation(runtime, device_id):
            _LOGGER.info("Enabling Engineering Mode: %s", entity_id)
            await device_io._call(runtime, "switch", "turn_on", {"entity_id": entity_id})
            await asyncio.sleep(device_io.SETTLE_SECONDS)
            if recovery._switch_state(runtime, entity_id) != "on":
                raise ValueError("The device has not reported Engineering Mode on")
    except ValueError as error:
        _failed(runtime, device_id, entity_id, previous, report, error, attempted_at)
    else:
        # Keep a cooldown even after an optimistic on report; the radar may revert.
        runtime._engineering_attempts[device_id] = {
            "entity_id": entity_id,
            "failures": 0,
            "retry_at": attempted_at + RETRY_DELAYS[0],
        }
        runtime._engineering_status.pop(device_id, None)


def _failed(runtime, device_id, entity_id, previous, report, error, attempted_at):
    failures = previous.get("failures", 0) if previous.get("entity_id") == entity_id else 0
    delay = RETRY_DELAYS[min(failures, len(RETRY_DELAYS) - 1)]
    runtime._engineering_attempts[device_id] = {
        "entity_id": entity_id,
        "failures": failures + 1,
        "retry_at": attempted_at + delay,
    }
    report.update(
        status="retrying",
        message=f"Engineering Mode could not be enabled. Retrying on a later check, after at least {delay // 60} minutes. Check the device connection if this continues.",
        error=str(error),
    )
    _LOGGER.warning("Could not enable %s; retry in %ss: %s", entity_id, delay, error)
