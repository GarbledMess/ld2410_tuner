"""Runtime state and explicit integration API."""

from __future__ import annotations

import asyncio
from collections import OrderedDict, defaultdict
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from ..calibration import service as calibration
from ..const import STORE_DELAY
from ..history import labels as manual_training
from ..history import recording
from ..presence import autolabelling
from ..presentation import charts
from ..presentation import snapshots as presentation
from . import discovery


class TunerRuntime:
    """Own state and expose a stable API; feature modules receive it explicitly."""

    def __init__(self, hass: HomeAssistant, store: Store, data: dict[str, Any]) -> None:
        self.hass = hass
        self.store = store
        self.data = data
        self.unsub = None
        self.unsub_registry = None
        self.unsub_retention = None
        self.unsub_sampling = None
        self.data.setdefault("devices", {})
        self._save_task: asyncio.Task | None = None
        self._timeout_tasks: dict[str, asyncio.Task] = {}
        self._applying: set[str] = set()
        self._live: dict[str, dict[str, float]] = defaultdict(dict)
        self._auto_runtime: dict[str, dict[str, Any]] = {}
        self._history_runtime: dict[str, dict[str, Any]] = {}
        self._history_cache = OrderedDict()
        self._history_jobs = {}
        self._learning_jobs = {}
        self._cleanup_task = None

    def _schedule_save(self) -> None:
        if self._save_task and not self._save_task.done():
            return
        self._save_task = self.hass.async_create_task(self._delayed_save())

    async def _delayed_save(self) -> None:
        await asyncio.sleep(STORE_DELAY)
        await self.store.async_save(self.data)

    subscribe_state_changes = discovery.subscribe_state_changes
    handle_registry_update = discovery.handle_registry_update
    refresh_devices = discovery.refresh_devices
    handle_state_change = discovery.handle_state_change
    sample_devices = autolabelling.sample_devices
    _record_history_sample = recording._record_history_sample
    _flush_history_block = recording._flush_history_block
    _enforce_history_retention = recording._enforce_history_retention
    async_clean_history = recording.async_clean_history
    _compact_history_labels = staticmethod(manual_training._compact_history_labels)
    flush_history = recording.flush_history
    _iter_history_samples = recording._iter_history_samples
    label_history_range = manual_training.label_history_range
    _manual_history_state = staticmethod(manual_training._manual_history_state)
    _history_label_at = staticmethod(manual_training._history_label_at)
    history_series_multi = charts.history_series_multi
    _find_threshold_entity = calibration._find_threshold_entity
    set_gate_threshold = calibration.set_gate_threshold
    _classify_auto = autolabelling._classify_auto
    _update_auto_state = autolabelling._update_auto_state
    auto_learning_summary = staticmethod(autolabelling.auto_learning_summary)
    record_auto_feedback = autolabelling.record_auto_feedback
    _migrate_device_samples = manual_training._migrate_device_samples
    _compress_histogram = staticmethod(manual_training._compress_histogram)
    _ensure_histograms = manual_training._ensure_histograms
    restore_timeouts = manual_training.restore_timeouts
    set_training_state = manual_training.set_training_state
    _close_training_interval = manual_training._close_training_interval
    _schedule_timeout = manual_training._schedule_timeout
    _timeout_worker = manual_training._timeout_worker
    clear_samples = manual_training.clear_samples
    _history_view = recording._history_view
    async_history_series = charts.async_history_series
    async_learn = calibration.async_learn
    _learn_once = calibration._learn_once
    _fit_history = calibration._fit_history
    _threshold_configuration = calibration._threshold_configuration
    apply = calibration.apply
    _apply_validated = calibration._apply_validated
    export_data = presentation.export_data
    snapshot = presentation.snapshot
