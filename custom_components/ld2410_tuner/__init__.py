"""Home Assistant lifecycle only; domain operations live in focused modules."""

from __future__ import annotations

import asyncio
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
    STORAGE_VERSION,
)
from .runtime.coordinator import TunerRuntime
from .runtime.websocket import _register_websocket_commands


class TunerStore(Store):
    """Store subclass that tolerates older on-disk schema versions.

    The actual data-shape migration (raw "samples" arrays -> bounded
    histograms) is performed lazily, per-device, by
    TunerRuntime._migrate_device_samples() the first time each device
    is touched. Home Assistant's Store base class raises
    NotImplementedError from _async_migrate_func() by default whenever
    the stored file's version doesn't match STORAGE_VERSION, even if
    the caller never intended Store itself to do the migration. This
    override just hands the old data back unchanged so async_load()
    succeeds; TunerRuntime takes it from there.
    """

    async def _async_migrate_func(self, old_major_version, old_minor_version, old_data):
        return old_data


async def async_setup_entry(hass: HomeAssistant, _entry: ConfigEntry) -> bool:
    if DOMAIN not in hass.data:
        await _start_runtime(hass)
    return True


async def _start_runtime(hass):
    store = TunerStore(hass, STORAGE_VERSION, STORAGE_KEY)
    data = await store.async_load() or {"devices": {}, "training": {}}
    runtime = TunerRuntime(hass, store, data)
    if await runtime.async_clean_history(persist=False):
        await store.async_save(runtime.data)
    hass.data[DOMAIN] = runtime

    _register_websocket_commands(hass)
    runtime.refresh_devices(er.async_get(hass))
    runtime.subscribe_state_changes()
    runtime.unsub_sampling = async_track_time_interval(
        hass, runtime.sample_devices, timedelta(seconds=AUTO_SAMPLE_INTERVAL)
    )
    runtime.restore_timeouts()
    runtime.unsub_registry = hass.bus.async_listen(
        EVENT_ENTITY_REGISTRY_UPDATED, runtime.handle_registry_update
    )
    runtime.unsub_retention = async_track_time_interval(
        hass, runtime._enforce_history_retention, HISTORY_RETENTION_CHECK_INTERVAL
    )

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
                    "js_url": f"/api/ld2410_tuner/static/ld2410-tuner-panel.js?v={INTEGRATION_VERSION}",
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
    if runtime._save_task:
        runtime._save_task.cancel()
        await asyncio.gather(runtime._save_task, return_exceptions=True)
    for device_id in runtime._history_runtime:
        runtime._flush_history_block(device_id)
    await runtime.store.async_save(runtime.data)
