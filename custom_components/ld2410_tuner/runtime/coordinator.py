"""Runtime state and explicit integration API."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict, defaultdict
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from ..calibration import automatic, comparison_jobs, timing_config
from ..calibration import service as calibration
from ..calibration import settings as learning_settings
from ..const import STORE_DELAY
from ..history import labels as manual_training
from ..history import recording, storage
from ..presence import autolabelling, rebuilds, sources
from ..presentation import charts, recordings
from ..presentation import snapshots as presentation
from ..rooms import application as room_application
from ..rooms import groups as room_groups
from ..rooms import jobs as room_jobs
from ..rooms import learning as room_learning
from ..rooms import room_labels, room_sources
from ..worker import priority
from . import discovery, schedule

STATE_SAVE_SECONDS = 300  # Recording state saves; history goes to the day logs.


class TunerRuntime:
    """Own state and expose a stable API; feature modules receive it explicitly."""

    @property
    def _physical_history_start(self):
        if hasattr(self, "_snapshot_history_start"):
            return self._snapshot_history_start
        return max(0, time.time() - recording.retention_seconds(self.data))

    def __init__(self, hass: HomeAssistant, store: Store, data: dict[str, Any]) -> None:
        self.hass = hass
        self.store = store
        self.data = data
        self.unsub = None
        self.unsub_registry = None
        self.unsub_retention = None
        self.unsub_sampling = None
        self.unsub_engineering = None
        self._engineering_task = None
        self._engineering_status = {}
        self._engineering_attempts = {}
        self.unsub_nightly = None
        self._nightly_task = None
        self.data.setdefault("devices", {})
        for device in self.data["devices"].values():
            autolabelling.migrate_segments(device)
        self._save_task: asyncio.Task | None = None
        self._timeout_tasks: dict[str, asyncio.Task] = {}
        self._applying: set[str] = set()
        self._live: dict[str, dict[str, float]] = defaultdict(dict)
        self._auto_runtime: dict[str, dict[str, Any]] = {}
        self._source_runtime: dict[str, dict[str, Any]] = {}
        self._room_sensor_open: dict[str, dict[str, Any]] = {}
        self._room_sensor_checked: set[str] = set()
        self._history_runtime: dict[str, dict[str, Any]] = {}
        # The recorded history (history/daystore.py); None until the integration opens it.
        self._days = None
        self._history_cache = OrderedDict()
        # Device id -> history.label_counts.Counts behind its histograms.
        self._label_counts = {}
        # Device id -> background task rebuilding histograms once its counts are kept.
        self._label_rebuilds = {}
        self._history_jobs = {}
        self._recording_jobs = {}
        self._learning_jobs = {}
        self._comparison_jobs = {}
        self._room_jobs = {}
        # Group id -> cached "Seen by" episodes and their rebuilds (rooms/seen_by.py).
        self._room_attribution = {}
        self._room_attribution_jobs = {}
        self._room_attribution_semaphore = asyncio.Semaphore(1)
        # Group id -> (inputs key, published room estimate) (rooms/room_labels.py).
        self._room_estimates = {}
        # Member id -> its newest sample and recent guess runs (rooms/room_labels.py).
        self._room_recent = {}
        self._room_membership = None
        # What the entity registry held when last read (runtime/discovery.py).
        self._entity_index = None
        # Reference rebuilds (presence/rebuilds.py) and the attribution cache
        # they share with Seen by: device id -> (inputs key, found).
        self._reference_pending: set[str] = set()
        self._reference_task = None
        self._reference_running: set[str] = set()
        self._reference_closed = False
        self._room_learning_jobs = {}
        self._comparison_cache = {}
        self._comparison_semaphore = asyncio.Semaphore(2)
        self._cleanup_task = None
        self._storage_lock = asyncio.Lock()
        self._storage_status = {}
        self._save_dirty = False
        self._flush_task = None
        self._state_saved = None

    def _schedule_flush(self) -> None:
        """Append recorded changes to the day logs shortly (executor): recording needs no JSON
        save, only its log appended."""
        if self._flush_task and not self._flush_task.done():
            return
        self._flush_task = self.hass.async_create_background_task(
            self._delayed_flush(), "ld2410_tuner history flush"
        )

    @priority.background
    async def _delayed_flush(self) -> None:
        from ..worker import flushing

        await asyncio.sleep(STORE_DELAY)
        if self._days is not None:
            await flushing.flush(self)

    def _schedule_state_save(self, now) -> None:
        """Recording state in the JSON store (classifications, live histograms) is saved at
        most every ``STATE_SAVE_SECONDS``: it is rebuilt from history after a crash."""
        if self._state_saved is None:
            self._state_saved = now
        elif now - self._state_saved >= STATE_SAVE_SECONDS:
            self._state_saved = now
            self._schedule_save()

    def _schedule_save(self) -> None:
        self._save_dirty = True
        if self._save_task and not self._save_task.done():
            return
        self._save_task = self.hass.async_create_task(self._delayed_save())

    @priority.background
    async def _delayed_save(self) -> None:
        while self._save_dirty:
            self._save_dirty = False
            await asyncio.sleep(STORE_DELAY)
            try:
                await self.async_save()
            except ValueError:
                return  # Reported in the panel; a settings change can retry.

    async_save = storage.async_save
    configure_room = room_groups.configure
    remove_room = room_groups.remove
    label_room_range = room_labels.label_room_range
    edit_room_label = room_labels.edit_room_label
    room_estimate_feedback = room_labels.room_estimate_feedback
    configure_room_sources = room_sources.configure_room_sources
    assess_room = room_jobs.start
    learn_set = room_learning.start_set
    apply_set = room_application.apply
    compare_results = comparison_jobs.compare_results
    configure_timing = timing_config.configure
    configure_storage = storage.configure
    trim_storage = storage.trim
    set_recording = storage.set_recording
    configure_presence_sources = sources.configure
    configure_learning_schedule = schedule.configure
    configure_automatic_weight = learning_settings.configure
    configure_device_auto_apply = automatic.configure_device
    nightly_tick = schedule.tick

    subscribe_state_changes = discovery.subscribe_state_changes
    handle_registry_update = discovery.handle_registry_update
    refresh_devices = discovery.refresh_devices
    handle_state_change = discovery.handle_state_change
    sample_devices = autolabelling.sample_devices
    async_sample_devices = autolabelling.async_sample_devices
    _record_history_sample = recording._record_history_sample
    _enforce_history_retention = recording._enforce_history_retention
    async_clean_history = recording.async_clean_history
    _compact_history_labels = staticmethod(manual_training._compact_history_labels)
    flush_history = recording.flush_history
    label_history_range = manual_training.label_history_range
    edit_history_label = manual_training.edit_history_label
    _manual_history_state = staticmethod(manual_training._manual_history_state)
    _history_label_at = staticmethod(manual_training._history_label_at)
    history_series_multi = charts.history_series_multi
    _find_threshold_entity = calibration._find_threshold_entity
    set_gate_threshold = calibration.set_gate_threshold
    _classify_auto = autolabelling._classify_auto
    _update_auto_state = autolabelling._update_auto_state
    auto_learning_summary = staticmethod(autolabelling.auto_learning_summary)
    record_auto_feedback = autolabelling.record_auto_feedback
    refresh_references = rebuilds.refresh_references
    _compress_histogram = staticmethod(manual_training._compress_histogram)
    _ensure_histograms = manual_training._ensure_histograms
    restore_timeouts = manual_training.restore_timeouts
    set_training_state = manual_training.set_training_state
    _close_training_interval = manual_training._close_training_interval
    _schedule_timeout = manual_training._schedule_timeout
    _timeout_worker = manual_training._timeout_worker
    clear_samples = manual_training.clear_samples
    _history_view = recording._history_view
    async_recording_days = recordings.async_recording_days
    async_recording_rows = recordings.async_recording_rows
    async_history_series = charts.async_history_series
    async_room_series = charts.async_room_series
    start_learning = calibration.start_learning
    async_learn = calibration.async_learn
    _learn_once = calibration._learn_once
    _fit_history = calibration._fit_history
    _threshold_configuration = calibration._threshold_configuration
    apply = calibration.apply
    _apply_validated = calibration._apply_validated
    export_data = presentation.export_data
    async_export_data = presentation.async_export_data
    async_export_file = presentation.async_export_file
    snapshot = presentation.snapshot
