"""Recording isolation, confidence ageing and total serialized-file budgets."""

import json
import random
import sys
import time
import types
import unittest
from copy import deepcopy
from unittest.mock import AsyncMock, patch

import legacy_formats
import pytest
import test_tuner as harness
from tuner_under_test.history import budget, daystore

policy = sys.modules["tuner_under_test.history.policy"]
storage = sys.modules["tuner_under_test.history.storage"]
spans = sys.modules["tuner_under_test.history.spans"]
schedule = sys.modules["tuner_under_test.runtime.schedule"]
NOW = 2_000_000_000


@pytest.mark.parametrize(
    "confidence,days",
    [(0, 7), (49, 7), (50, 7), (62.5, 12.75), (75, 18.5), (99, 29.54), (100, 30)],
)
def test_confidence_cutoff_rises_continuously_to_expiry(confidence, days):
    """Unlabelled time older than the cutoff is thinned: from ``thin_after_days`` for a guess
    at or under the minimum confidence, rising linearly to ``automatic_days`` for a certain one."""
    cutoff = budget._cutoff(NOW, False, confidence, policy.DEFAULTS)
    assert cutoff == pytest.approx(NOW - days * policy.DAY)


def test_human_time_is_kept_for_the_human_retention_whatever_the_confidence():
    for confidence in (0, 100):
        assert budget._cutoff(NOW, True, confidence, policy.DEFAULTS) == NOW - 30 * policy.DAY


def populated():
    """Two radars of old-format history to convert: 3 hours of random energies, the newest
    80 minutes labelled present, and a radar guess that is weak in the middle third."""
    rng = random.Random(12)
    devices = {}
    start = NOW - 12000
    runs = [
        (start, start + 3600, "present", 18, 0),
        (start + 3600, start + 7200, "present", 4, 0),
        (start + 7200, start + 10800, "present", 18, 0),
    ]
    for name in ("a", "b"):
        blocks = [
            legacy_formats.sampled_block(
                start + block * 360,
                [(i * 6, [rng.randrange(101) for _ in range(18)]) for i in range(60)],
            )
            for block in range(30)
        ]
        devices[name] = {
            "history": blocks,
            "guess_runs": legacy_formats.guess_chunks(runs),
            "history_labels": [{"start": NOW - 4800, "end": NOW, "state": "present"}],
        }
    return {"devices": devices}


def test_budget_counts_non_ascii_metadata_and_refuses_impossible_floor(tmp_path):
    store = daystore.DayStore(tmp_path / "history")
    data = {"devices": {}, "metadata": "雷达" * 1000}
    prepared, changes, report = budget.prepare_days(store, data, NOW)
    envelope = {
        "version": 2,
        "minor_version": harness.constants.STORAGE_MINOR_VERSION,
        "key": "ld2410_tuner.data",
        "data": prepared,
    }
    assert report["used_bytes"] == len(json.dumps(envelope, indent=2).encode("utf-8"))
    assert not changes
    with pytest.raises(ValueError, match="remaining settings"):
        budget.prepare_days(store, data, NOW, target=100)
    assert data["metadata"] == "雷达" * 1000


def full_storage_bytes(data):
    """The previous ``storage_bytes``: the whole indented store encoded on every save."""
    envelope = {
        "version": 2,
        "minor_version": harness.constants.STORAGE_MINOR_VERSION,
        "key": "ld2410_tuner.data",
        "data": data,
    }
    return len(json.dumps(envelope, indent=2).encode("utf-8"))


def test_storage_bytes_is_the_full_indented_encoding():
    data = {"devices": {"a": {"name": "雷达", "history_labels": [{"start": 1.5}]}}}
    assert budget.storage_bytes(data) == full_storage_bytes(data)


def test_copy_data_is_a_deep_copy():
    data = {"devices": {"a": {"history_labels": [{"start": 1, "end": 2, "state": "present"}]}}}
    copied = budget.copy_data(data)
    assert copied == data
    data["devices"]["a"]["history_labels"][0]["state"] = "not_present"
    data["devices"]["a"]["history_labels"].append({"start": 3})
    assert copied["devices"]["a"]["history_labels"] == [{"start": 1, "end": 2, "state": "present"}]


def test_copy_data_keeps_tuples_and_scalar_keys_as_they_are():
    data = {"runs": [(1.5, 2.5, "present")], 3: None, "nested": {"x": ((1,), [2])}}
    copied = budget.copy_data(data)
    assert copied == data
    assert type(copied["runs"][0]) is tuple and type(copied["nested"]["x"][1]) is list
    assert copied["nested"]["x"][1] is not data["nested"]["x"][1]


def test_copy_data_refuses_unbounded_data_before_copying_it_all():
    deep = []
    for _ in range(25):
        deep = [deep]
    with pytest.raises(ValueError):
        budget.copy_data({"deep": deep})
    with pytest.raises(ValueError):
        budget.copy_data({"wide": list(range(10))}, max_nodes=5)


@pytest.mark.parametrize(
    "change",
    [
        {"thin_after_days": 30},
        {"automatic_days": 31},
        {"human_days": -1},
        {"minimum_confidence": 101},
        {"max_mib": 0},
        {"max_mib": float("nan")},
        {"human_days": True},
        {"extra": 1},
    ],
)
def test_policy_rejects_invalid_or_ambiguous_settings(change):
    with pytest.raises(ValueError):
        policy.validate({**policy.DEFAULTS, **change})


class StorageRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        clock = patch.object(time, "time", return_value=NOW)
        clock.start()
        self.addCleanup(clock.stop)
        harness.RuntimeTests.setUp(self)

    asyncTearDown = harness.RuntimeTests.asyncTearDown
    sample = harness.RuntimeTests.sample

    def test_snapshot_exposes_history_save_progress(self):
        self.runtime._history_saved_at = NOW - 600
        self.runtime._history_waiting_since = NOW - 300
        self.assertEqual(
            self.runtime.snapshot()["history_save"],
            {"saved_at": NOW - 600, "waiting_since": NOW - 300, "stall_seconds": 900},
        )

    def finished_sample(self, timestamp):
        from tuner_under_test.history import ingest

        self.sample(timestamp)
        ingest.end(self.runtime, "a", timestamp + 6)

    def migrate_history(self, data):
        from tuner_under_test.history import convert

        convert.run(data, self.runtime._days, NOW + policy.DAY)
        self.runtime.data = data

    async def test_paused_recording_cannot_update_history_or_learning_baselines(self):
        self.sample(self.now)
        self.runtime.set_training_state("a", "present")
        with patch.object(time, "time", return_value=self.now + 3):
            self.runtime.set_recording("a", False)
        before = deepcopy(self.device)
        self.sample(self.now + 6, 90)
        self.runtime._record_history_sample("a", {"g0_move": 90}, self.now + 7)
        assert self.device == before
        assert self.runtime._live["a"]["g0_move"] == 90
        assert self.device["training_state"] == "unknown"
        with self.assertRaisesRegex(ValueError, "Enable recording"):
            self.runtime.set_training_state("a", "present")
        self.runtime.set_recording("a", True)
        self.sample(self.now + 12, 30)
        with patch.object(time, "time", return_value=self.now + 13):
            cells = list(spans.events(self.runtime, "a"))
        assert [
            (round(c.start - self.now), round(c.end - self.now), c.values[0]) for c in cells
        ] == [(0, 3, 10), (12, 13, 30)]  # Nothing is recorded while paused.

    def test_new_discovery_is_opt_in_and_existing_devices_keep_their_preference(self):

        self.registry.entities = {
            name: types.SimpleNamespace(entity_id=name, device_id=did, domain="sensor")
            for name, did in (
                ("sensor.old_g0_move_energy", "a"),
                ("sensor.new_g0_move_energy", "b"),
            )
        }
        self.runtime.refresh_devices(self.registry)
        assert self.device.get("recording_enabled", True)
        assert self.runtime.data["devices"]["b"]["recording_enabled"] is False
        self.runtime.set_recording("b", True)
        self.runtime.refresh_devices(self.registry)
        assert self.runtime.data["devices"]["b"]["recording_enabled"]

    async def test_global_settings_apply_to_all_devices_and_survive_saved_data(self):
        data = populated()
        self.migrate_history(data)
        settings = {**policy.DEFAULTS, "human_days": 90, "max_mib": 2}
        await self.runtime.configure_storage(settings)
        saved = self.runtime.store.async_save.call_args.args[0]
        assert saved["storage_settings"] == settings
        assert set(saved["devices"]) == {"a", "b"}
        assert budget.storage_bytes(saved) <= 2 * policy.MIB
        assert "settings" not in saved["devices"]["a"]
        assert storage.summary(self.runtime)["settings"] == settings

    async def test_ordinary_save_trims_under_the_total_file_ceiling(self):
        device = populated()["devices"]["a"]
        data = {"devices": {str(index): deepcopy(device) for index in range(50)}}
        self.migrate_history(data)
        await self.runtime.async_clean_history(persist=False)  # As at startup.
        for key in data["devices"]:  # Labelled radars keep their summaries (a label edit).
            await self.runtime.label_history_range(key, NOW - 4800, NOW, "present")
        await harness.settled(self.runtime)
        canonical = self.runtime.data
        # Require physical trimming even when disposable summary indexes are evicted first.
        physical_bytes = sum(
            path.stat().st_size
            for path in self.runtime._days.root.rglob("*")
            if path.suffix in (".day", ".log")
        )
        limit = budget.storage_bytes(canonical) + physical_bytes - 30000
        assert limit > policy.MIB
        canonical["storage_settings"] = {**policy.DEFAULTS, "max_mib": limit / policy.MIB}
        self.runtime.data = canonical
        with patch("tuner_under_test.history.storage.time.time", return_value=NOW):
            report = await self.runtime.async_save()
        saved = self.runtime.store.async_save.call_args.args[0]
        assert budget.storage_bytes(saved) + self.runtime._days.size() <= limit
        assert report["removed_automatic"] > 0
        assert report["removed_human"] == 0
        assert report["used_bytes"] == budget.storage_bytes(saved) + self.runtime._days.size()
        assert self.runtime.data == saved

    async def test_small_limit_never_writes_or_commits_an_impossible_configuration(self):
        self.runtime.data["metadata"] = "x" * (2 * policy.MIB)
        with self.assertRaisesRegex(ValueError, "remaining settings"):
            await self.runtime.configure_storage({**policy.DEFAULTS, "max_mib": 1})
        self.runtime.store.async_save.assert_not_awaited()
        assert "storage_settings" not in self.runtime.data
        assert not self.runtime._storage_status["blocked"]
        self.runtime.data["storage_settings"] = {**policy.DEFAULTS, "max_mib": 1}
        with self.assertRaises(ValueError):
            await self.runtime.async_save()
        assert self.runtime._storage_status["blocked"]
        self.sample(self.now)
        assert not self.runtime._history_runtime
        await self.runtime.configure_storage({**policy.DEFAULTS, "max_mib": 3})
        assert not self.runtime._storage_status.get("blocked")

    async def test_a_save_validates_the_stored_data_off_the_event_loop(self):
        # Each walk of a 12-radar home's data takes about 0.1 s: the loop only takes the save's
        # private copy (bounded, ``budget.copy_data``), which is validated in the executor.
        import threading

        storage_values = sys.modules["tuner_under_test.worker.storage_values"]
        loop_thread, walks = threading.current_thread(), []
        real = storage_values.validate

        def spy(value, **kwargs):
            if "devices" in value or "devices" in value.get("data", {}):  # Not a flush's {}.
                walks.append(threading.current_thread() is loop_thread)
            return real(value, **kwargs)

        self.finished_sample(self.now - 60)
        with patch.object(storage_values, "validate", spy):
            await self.runtime.async_save()
        assert walks.count(True) == 0
        assert walks.count(False) >= 1  # The copy sent to the worker is still checked.

    async def test_manual_trim_flushes_pending_and_reports_age_removals(self):
        self.finished_sample(self.now - 10 * policy.DAY)
        report = await self.runtime.trim_storage()
        assert not list(spans.events(self.runtime, "a"))
        assert report["removed_automatic"] == 6
        await self.runtime.async_save()
        assert self.runtime._storage_status["last_trim"]["removed_automatic"] == 6
        for invalid in (-1, float("nan"), 101, True):
            with self.assertRaises(ValueError):
                await self.runtime.trim_storage(invalid)

    async def test_trim_retries_a_concurrent_label_edit_without_overwriting_it(self):
        self.finished_sample(self.now - 10 * policy.DAY)
        calls = 0

        async def race(request):
            nonlocal calls
            if request["operation"] != "maintenance":
                return
            if calls == 0:
                await self.runtime.label_history_range(
                    "a", self.now - 11 * policy.DAY, self.now, "present"
                )
            calls += 1

        self.worker_transport.after = race
        await self.runtime.trim_storage()
        assert calls == 2
        assert sum(span.end - span.start for span in spans.events(self.runtime, "a")) == 6
        assert self.device["history_labels"][0]["state"] == "present"

    async def test_no_stale_write_when_every_cleanup_races(self):
        self.finished_sample(self.now - 10 * policy.DAY)

        async def race(request):
            if request["operation"] == "maintenance":
                self.device["label_revision"] = self.device.get("label_revision", 0) + 1

        self.worker_transport.after = race
        with self.assertRaisesRegex(ValueError, "changed during trimming"):
            await self.runtime.trim_storage()
        self.runtime.store.async_save.assert_not_awaited()
        assert sum(span.end - span.start for span in spans.events(self.runtime, "a")) == 6

    async def test_changes_during_save_are_followed_by_another_save(self):
        self.runtime._schedule_save = harness.mod.TunerRuntime._schedule_save.__get__(self.runtime)
        original = self.runtime.async_save

        async def save():
            result = await original()
            if self.runtime.store.async_save.await_count == 1:
                self.runtime.data["new_setting"] = True
                self.runtime._schedule_save()
            return result

        self.runtime.async_save = save
        with patch("tuner_under_test.runtime.coordinator.STORE_DELAY", 0):
            self.runtime._schedule_save()
            await self.runtime._save_task
        assert self.runtime.store.async_save.await_count == 2
        assert self.runtime.store.async_save.call_args.args[0]["new_setting"]

    async def test_save_snapshot_is_detached_from_concurrent_native_recording(self):
        """Metadata and native day snapshots stay detached while recording changes live state."""
        self.finished_sample(self.now - 10 * policy.DAY)
        self.sample(self.now - 6)
        live = self.device
        live["histograms"] = {"g0_move": {"present": [0] * 101, "not_present": [0] * 101}}
        live.setdefault("history_labels", [])
        expected = deepcopy(self.runtime.data)
        seen = {}

        async def concurrent(request):
            if request["operation"] == "maintenance" and not seen:
                seen["candidate"] = request["data"]
                date = self.runtime._days.days("a")[-1]
                seen["day"] = self.runtime._days.day("a", date, until=NOW)
                seen["day_before"] = deepcopy(seen["day"])
                self.sample(self.now, 81)
                live["histograms"]["g0_move"]["present"][3] += 7
                live["history_labels"].append({"start": 1, "end": 2, "state": "present"})

        self.worker_transport.after = concurrent
        with patch("tuner_under_test.history.storage.time.time", return_value=NOW):
            await self.runtime.async_save()
        saved = self.runtime.store.async_save.call_args.args[0]
        assert seen["candidate"] == expected
        assert seen["day"] == seen["day_before"]
        assert saved == expected
        assert live["histograms"]["g0_move"]["present"][3] == 7
        assert live["history_labels"][-1]["start"] == 1
        with patch.object(time, "time", return_value=self.now + 1):
            assert list(spans.events(self.runtime, "a"))[-1].values[0] == 81

    async def test_live_data_never_aliases_a_trimmed_save(self):
        """After a cleaning save, recording keeps changing live lists; the written data must
        not change with them."""
        self.finished_sample(self.now - 10 * policy.DAY)
        self.sample(self.now - 6)
        self.device["histograms"] = {"g0_move": {"present": [0] * 101, "not_present": [0] * 101}}
        await self.runtime.trim_storage()
        saved = self.runtime.store.async_save.call_args.args[0]
        written = deepcopy(saved)
        self.sample(self.now, 81)
        self.device.setdefault("history_labels", []).append(
            {"start": 1, "end": 2, "state": "present"}
        )
        self.device["histograms"]["g0_move"]["present"][5] += 1
        assert saved == written

    async def test_cleanup_expires_labels_older_than_the_human_retention(self):
        """Regression: on day files nothing expired labels by age, so they grew forever."""
        day = policy.DAY
        self.device["history_labels"] = [
            {"start": NOW - 40 * day, "end": NOW - 35 * day, "state": "present"},
            {"start": NOW - 31 * day, "end": NOW - 29 * day, "state": "not_present"},
            {"start": NOW - 2 * day, "end": NOW - day, "state": "present"},
        ]
        revision = self.device.get("label_revision", 0)
        await self.runtime.trim_storage()
        assert self.device["history_labels"] == [
            {"start": NOW - 30 * day, "end": NOW - 29 * day, "state": "not_present"},
            {"start": NOW - 2 * day, "end": NOW - day, "state": "present"},
        ]
        assert self.device["label_revision"] == revision + 1
        saved = self.runtime.store.async_save.call_args.args[0]
        assert saved["devices"]["a"]["history_labels"] == self.device["history_labels"]

    async def test_overnight_skips_paused_devices(self):
        self.device["recording_enabled"] = False
        self.runtime.data["learning_schedule"] = {"enabled": True}
        self.runtime.async_learn = AsyncMock()
        await schedule._run(self.runtime, "2026-09-27")
        self.runtime.async_learn.assert_not_awaited()
