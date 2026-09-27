"""Bounded recovery before learning using optional standard ESPHome entities."""

import asyncio
import math
import time

from homeassistant.helpers import entity_registry as er

from . import device_io

COOLDOWN_SECONDS = 60
RESTART_WAIT_SECONDS = 3


class SettingsUnavailable(ValueError):
    """Exposed radar settings have not loaded; a query/restart may recover them."""


def _configuration(runtime, device_id):
    try:
        entities, current = runtime._threshold_configuration(device_id)
    except ValueError as error:
        if "Maximum distance gate is unavailable" in str(error):
            raise SettingsUnavailable(str(error)) from error
        raise
    if len(current) != len(entities) or any(
        not math.isfinite(v) or not 0 <= v <= 100 or int(v) != v for v in current.values()
    ):
        raise SettingsUnavailable("Radar threshold settings are unavailable")
    return entities, current


def capability(runtime, device_id, domain, suffixes):
    matches = []
    for entry in er.async_get(runtime.hass).entities.values():
        if (
            entry.device_id != device_id
            or entry.domain != domain
            or getattr(entry, "disabled_by", None)
        ):
            continue
        names = [
            entry.entity_id,
            str(getattr(entry, "original_name", "") or "").lower().replace(" ", "_"),
        ]
        if any(name.endswith(suffixes) for name in names):
            matches.append(entry.entity_id)
    return matches[0] if len(matches) == 1 else None


def _switch_state(runtime, entity_id):
    state = runtime.hass.states.get(entity_id) if entity_id else None
    return state.state if state and state.state in ("on", "off") else None


def _stage(runtime, report, stage):
    report.update(status="running", stage=stage)
    report.setdefault("steps", []).append(stage)
    runtime._schedule_save()


async def _switch(runtime, entity_id, state):
    await device_io._call(runtime, "switch", f"turn_{state}", {"entity_id": entity_id})
    await asyncio.sleep(RESTART_WAIT_SECONDS)


async def _restore_bluetooth(runtime, report):
    pending = report.get("restore_bluetooth")
    if not pending:
        return
    await _switch(runtime, pending["entity_id"], pending["state"])
    if _switch_state(runtime, pending["entity_id"]) != pending["state"]:
        raise SettingsUnavailable(
            "Bluetooth restoration was not confirmed; restore its original setting in Home Assistant"
        )
    report.pop("restore_bluetooth", None)
    runtime._schedule_save()


async def _cycle_bluetooth(runtime, entity_id, report):
    original = _switch_state(runtime, entity_id)
    if original is None:
        raise SettingsUnavailable("Bluetooth state is unknown; recovery cannot safely cycle it")
    report["restore_bluetooth"] = {"entity_id": entity_id, "state": original}
    await runtime.async_save()
    try:
        await _switch(runtime, entity_id, "off" if original == "on" else "on")
    finally:
        await asyncio.shield(_restore_bluetooth(runtime, report))


async def _restore_engineering(runtime, entity_id, original):
    if entity_id and original == "on":
        await device_io._call(runtime, "switch", "turn_on", {"entity_id": entity_id})
        await asyncio.sleep(device_io.SETTLE_SECONDS)


def _ready(runtime, device_id):
    try:
        return _configuration(runtime, device_id)
    except SettingsUnavailable:
        return None


async def _queries(runtime, device_id, report, button):
    if not button:
        return None
    for _ in range(device_io.RECOVERY_ATTEMPTS):
        _stage(runtime, report, "query_parameters")
        await device_io.refresh_parameters(runtime, button)
        result = _ready(runtime, device_id)
        if result:
            return result
    return None


async def _restart_steps(runtime, device_id, report, button):
    radar = capability(runtime, device_id, "button", ("radar_restart", "ld2410_restart"))
    bluetooth = capability(runtime, device_id, "switch", ("bluetooth",))
    engineering = capability(runtime, device_id, "switch", ("engineering_mode",))
    original = _switch_state(runtime, engineering)
    for kind, entity_id in (("radar_restart", radar), ("bluetooth_cycle", bluetooth)):
        if not entity_id:
            continue
        _stage(runtime, report, kind)
        try:
            await _restart(runtime, kind, entity_id, report)
        finally:
            await _restore_engineering(runtime, engineering, original)
        result = await _queries(runtime, device_id, report, button) or _ready(runtime, device_id)
        if result:
            return result
    raise SettingsUnavailable(
        "Radar settings are still unavailable after recovery. Check connectivity, or expose Query Params / Radar Restart / Bluetooth on this device."
    )


async def _restart(runtime, kind, entity_id, report):
    if kind == "bluetooth_cycle":
        await _cycle_bluetooth(runtime, entity_id, report)
    else:
        await device_io._call(runtime, "button", "press", {"entity_id": entity_id})
        await asyncio.sleep(RESTART_WAIT_SECONDS)


async def prepare_learning(runtime, device_id):
    device = runtime.data["devices"][device_id]
    previous = device.get("configuration_recovery", {})
    with device_io.device_operation(runtime, device_id):
        if previous.get("restore_bluetooth"):
            await _restore_bluetooth(runtime, previous)
        result = _ready(runtime, device_id)
        if result:
            return result
        if time.time() < previous.get("retry_after", 0):
            raise SettingsUnavailable("Radar recovery is cooling down; retry in about one minute")
        report = {
            "status": "running",
            "started_at": time.time(),
            "retry_after": time.time() + COOLDOWN_SECONDS,
            "steps": [],
        }
        device["configuration_recovery"] = report
        try:
            await runtime.async_save()
            button = device_io.query_button(runtime, device_id)
            result = await _queries(runtime, device_id, report, button)
            result = result or await _restart_steps(runtime, device_id, report, button)
            report["status"] = "recovered"
            return result
        except asyncio.CancelledError:
            report.update(status="interrupted", error="Recovery interrupted")
            raise
        except Exception as error:
            report.update(status="failed", error=str(error))
            raise
        finally:
            report["finished_at"] = time.time()
            runtime._schedule_save()
