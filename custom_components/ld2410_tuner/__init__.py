"""Home Assistant lifecycle only; domain operations live in focused modules."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import timedelta
from pathlib import Path

from homeassistant.components import frontend
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_registry import EVENT_ENTITY_REGISTRY_UPDATED
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store

from .const import (
    AUTO_SAMPLE_INTERVAL,
    DOMAIN,
    HISTORY_RETENTION_CHECK_INTERVAL,
    INTEGRATION_VERSION,
    STORAGE_KEY,
    STORAGE_MINOR_VERSION,
    STORAGE_VERSION,
)
from .history import convert, ingest
from .history.daystore import DayStore
from .presence import rebuilds, reference_training
from .runtime import engineering, schedule
from .runtime.coordinator import TunerRuntime
from .runtime.websocket import _register_websocket_commands


class TunerStore(Store):
    """Store subclass that loads older on-disk schema versions as they are.

    Home Assistant's Store raises NotImplementedError from _async_migrate_func() whenever the
    stored file's version differs from STORAGE_VERSION. Nothing needs reshaping here: history
    of any earlier layout is converted into day files once at startup (``history/convert.py``),
    in the executor, before anything reads it.
    """

    async def _async_migrate_func(self, old_major_version, old_minor_version, old_data):
        return old_data


def _open_history(runtime):
    """Recover the day store, then move any history still in the JSON store into it, once."""
    now = time.time()
    runtime._days.open(now)
    convert.run(runtime.data, runtime._days, now)


async def async_setup_entry(hass: HomeAssistant, _entry: ConfigEntry) -> bool:
    if DOMAIN not in hass.data:
        await _start_runtime(hass)
    return True


async def _start_runtime(hass):
    store = TunerStore(
        hass,
        STORAGE_VERSION,
        STORAGE_KEY,
        encoder=json.JSONEncoder,
        minor_version=STORAGE_MINOR_VERSION,
    )
    data = await store.async_load() or {"devices": {}, "training": {}}
    runtime = TunerRuntime(hass, store, data)
    runtime._days = DayStore(Path(hass.config.path(".storage", DOMAIN, "history")))
    await hass.async_add_executor_job(_open_history, runtime)
    schedule.restore(runtime)
    await reference_training.initialize(runtime)
    try:
        await runtime.async_save()
    except ValueError:
        pass  # Keep the panel available to correct an impossible storage limit.
    hass.data[DOMAIN] = runtime

    _register_websocket_commands(hass)
    runtime.refresh_devices(er.async_get(hass))
    runtime.subscribe_state_changes()
    engineering.start(runtime)
    runtime.unsub_sampling = async_track_time_interval(
        hass, runtime.sample_devices, timedelta(seconds=AUTO_SAMPLE_INTERVAL)
    )
    runtime.unsub_nightly = async_track_time_interval(
        hass, runtime.nightly_tick, timedelta(minutes=1)
    )
    runtime.restore_timeouts()
    runtime.unsub_registry = hass.bus.async_listen(
        EVENT_ENTITY_REGISTRY_UPDATED, runtime.handle_registry_update
    )
    runtime.unsub_retention = async_track_time_interval(
        hass, runtime._enforce_history_retention, HISTORY_RETENTION_CHECK_INTERVAL
    )
    # The first cleanup after an upgrade can thin every converted day: minutes on a small
    # machine. Setup never waits for it (Home Assistant cancels a slow setup); it runs now in
    # the background, like the hourly one.
    runtime._enforce_history_retention()

    static_path = Path(__file__).parent / "static"
    if not hass.data.get(f"{DOMAIN}_static_registered"):
        await hass.http.async_register_static_paths(
            [StaticPathConfig("/api/ld2410_tuner/static", str(static_path), cache_headers=False)]
        )
        hass.data[f"{DOMAIN}_static_registered"] = True

    if not frontend.async_panel_exists(hass, "ld2410-tuner"):
        frontend.async_register_built_in_panel(
            hass,
            component_name="custom",
            sidebar_title="LD2410 Tuner",
            sidebar_icon="mdi:radar",
            frontend_url_path="ld2410-tuner",
            require_admin=True,
            config={
                "_panel_custom": {
                    "name": "ld2410-tuner-panel",
                    "embed_iframe": False,
                    "trust_external": False,
                    # The entrypoint imports panel modules and must use HA's module loader.
                    "module_url": f"/api/ld2410_tuner/static/ld2410-tuner-panel.js?v={INTEGRATION_VERSION}",
                }
            },
        )


async def async_unload_entry(hass: HomeAssistant, _entry: ConfigEntry) -> bool:
    runtime = hass.data.pop(DOMAIN, None)
    await _stop_runtime(runtime)
    if frontend.async_panel_exists(hass, "ld2410-tuner"):
        frontend.async_remove_panel(hass, "ld2410-tuner")
    return True


async def _stop_runtime(runtime):
    if runtime:
        await _shutdown(runtime)


async def _shutdown(runtime):
    await engineering.stop(runtime)
    await schedule.stop(runtime)
    if runtime.unsub:
        runtime.unsub()
    if getattr(runtime, "unsub_registry", None):
        runtime.unsub_registry()
    if getattr(runtime, "unsub_retention", None):
        runtime.unsub_retention()
    if runtime.unsub_sampling:
        runtime.unsub_sampling()
    if runtime._cleanup_task:
        runtime._cleanup_task.cancel()
        await asyncio.gather(runtime._cleanup_task, return_exceptions=True)
    for task in runtime._timeout_tasks.values():
        task.cancel()
    runtime._timeout_tasks.clear()
    await rebuilds.stop(runtime)  # Drop queued reference rebuilds; stale marks are saved.
    for task in (runtime._save_task, runtime._flush_task):
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    ingest.stop(runtime, time.time())  # Nothing is observed while stopped.
    await runtime.async_save()
