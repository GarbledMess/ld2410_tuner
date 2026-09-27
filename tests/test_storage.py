"""Recording isolation, confidence ageing and total serialized-file budgets."""

import json
import random
import sys
import unittest
from copy import deepcopy
from unittest.mock import AsyncMock, patch

import pytest
import test_tuner as harness

budget = sys.modules["tuner_under_test.history.budget"]
cleanup = sys.modules["tuner_under_test.history.cleanup"]
policy = sys.modules["tuner_under_test.history.policy"]
storage = sys.modules["tuner_under_test.history.storage"]
schedule = sys.modules["tuner_under_test.runtime.schedule"]
NOW = 2_000_000_000


def row(confidence=80, value=20):
    return bytes([value] * 18 + [1, confidence])


@pytest.mark.parametrize(
    "days,confidence,keep",
    [
        (6.99, 0, True),
        (7, 49, False),
        (7, 50, True),
        (18.5, 74, False),
        (18.5, 75, True),
        (29.99, 100, True),
        (30, 100, False),
        (31, 100, False),
    ],
)
def test_confidence_cutoff_rises_continuously_to_expiry(days, confidence, keep):
    assert (
        policy.keep_automatic(NOW - days * policy.DAY, row(confidence), NOW, policy.DEFAULTS)
        == keep
    )


def test_age_cleanup_keeps_human_labels_ahead_of_automatic_confidence():
    samples = [
        (NOW - d * policy.DAY, row(c)) for d, c in ((31, 100), (20, 10), (19, 100), (8, 40), (2, 0))
    ]
    human_ts = samples[1][0]
    device = {
        "history": cleanup._encode(sorted(samples)),
        "history_labels": [{"start": human_ts, "end": human_ts + 1, "state": "present"}],
    }
    data, stats = cleanup.clean_history(device, [], NOW, 30 * policy.DAY, policy.DEFAULTS)
    retained = [ts for block in data["history"] for ts, _ in budget._rows(block)]
    assert retained == [samples[i][0] for i in (1, 2, 4)]
    assert sum(data["histograms"]["g0_move"]["present"]) == 1
    assert stats["expired_samples"] == 1
    assert stats["discarded_samples"] == 1
    again, _ = cleanup.clean_history(data, [], NOW, 30 * policy.DAY, policy.DEFAULTS)
    assert again == data


def populated():
    rng = random.Random(12)
    devices = {}
    for name in ("a", "b"):
        samples = []
        for index in range(1800):
            confidence = 20 if index < 600 else 90
            samples.append(
                (
                    NOW - 12000 + index * 6,
                    bytes([rng.randrange(101) for _ in range(18)] + [1, confidence]),
                )
            )
        devices[name] = {
            "history": cleanup._encode(samples),
            "history_legacy_histograms": {},
            "history_labels": [{"start": NOW - 4800, "end": NOW, "state": "present"}],
        }
    return {"devices": devices}


def test_total_size_trim_discards_low_confidence_globally_before_human():
    original, _, _ = budget.prepare(populated(), {}, NOW, clean=True)
    normalized = deepcopy(original)
    original_copy = deepcopy(original)
    target = budget.storage_bytes(normalized) - 15000
    prepared, changes, report = budget.prepare(original, {}, NOW, target=target)
    assert budget.storage_bytes(prepared) <= target
    assert report["removed_automatic"] > 0 and report["removed_human"] == 0
    assert original == original_copy
    assert changes
    remaining = [
        row[19]
        for device in prepared["devices"].values()
        for block in device["history"]
        for _, row in budget._rows(block)
    ]
    assert remaining.count(90) == 2400
    # Strong automatic readings are also removed before any human-labelled ones.
    human_only = deepcopy(normalized)
    for device in human_only["devices"].values():
        labelled = [
            (ts, sample)
            for block in device["history"]
            for ts, sample in budget._rows(block)
            if ts >= NOW - 4800
        ]
        device["history"] = cleanup._encode(labelled)
    target = budget.storage_bytes(human_only) - 10000
    prepared, _, report = budget.prepare(original, {}, NOW, target=target)
    assert budget.storage_bytes(prepared) <= target
    assert report["removed_automatic"] == 2400
    assert report["removed_human"] > 0
    assert all(
        sample[19] == 90
        for device in prepared["devices"].values()
        for block in device["history"]
        for _, sample in budget._rows(block)
    )


def test_budget_counts_non_ascii_metadata_and_refuses_impossible_floor():
    data = {"devices": {}, "metadata": "雷达" * 1000}
    prepared, changes, report = budget.prepare(data, {}, NOW)
    envelope = {"version": 2, "minor_version": 1, "key": "ld2410_tuner.data", "data": prepared}
    assert report["used_bytes"] == len(json.dumps(envelope, indent=2).encode("utf-8"))
    assert not changes
    with pytest.raises(ValueError, match="remaining settings"):
        budget.prepare(data, {}, NOW, target=100)
    assert data["metadata"] == "雷达" * 1000


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
    setUp = harness.RuntimeTests.setUp
    asyncTearDown = harness.RuntimeTests.asyncTearDown
    sample = harness.RuntimeTests.sample

    async def test_paused_recording_cannot_update_history_or_learning_baselines(self):
        self.sample(self.now)
        self.runtime.set_training_state("a", "present")
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
        assert len(self.runtime._history_runtime["a"]["samples"]) == 1
        assert self.device["history"] == before["history"]

    def test_new_discovery_is_opt_in_and_existing_devices_keep_their_preference(self):
        import types

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
        self.runtime.data["devices"] = data["devices"]
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
        data = {"devices": {str(index): deepcopy(device) for index in range(30)}}
        canonical, _, _ = budget.prepare(data, {}, NOW, clean=True)
        limit = budget.storage_bytes(canonical) - 30000
        assert limit > policy.MIB
        canonical["storage_settings"] = {**policy.DEFAULTS, "max_mib": limit / policy.MIB}
        self.runtime.data = canonical
        with patch("tuner_under_test.history.storage.time.time", return_value=NOW):
            report = await self.runtime.async_save()
        saved = self.runtime.store.async_save.call_args.args[0]
        assert budget.storage_bytes(saved) <= limit
        assert report["removed_automatic"] > 0
        assert report["removed_human"] == 0
        assert report["used_bytes"] == budget.storage_bytes(saved)
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

    async def test_manual_trim_flushes_pending_and_reports_age_removals(self):
        self.sample(self.now - 10 * policy.DAY)
        report = await self.runtime.trim_storage()
        assert not self.runtime._history_runtime["a"]["samples"]
        assert not self.device["history"]
        assert report["removed_automatic"] == 1
        await self.runtime.async_save()
        assert self.runtime._storage_status["last_trim"]["removed_automatic"] == 1
        for invalid in (-1, float("nan"), 101, True):
            with self.assertRaises(ValueError):
                await self.runtime.trim_storage(invalid)

    async def test_trim_retries_a_concurrent_label_edit_without_overwriting_it(self):
        self.sample(self.now - 10 * policy.DAY)
        self.runtime._flush_history_block("a")
        execute = self.hass.async_add_executor_job
        calls = 0

        async def race(fn, *args):
            nonlocal calls
            result = await execute(fn, *args)
            if calls == 0:
                self.runtime.label_history_range(
                    "a", self.now - 11 * policy.DAY, self.now, "present"
                )
            calls += 1
            return result

        self.hass.async_add_executor_job = race
        await self.runtime.trim_storage()
        assert calls == 2
        assert sum(block["count"] for block in self.device["history"]) == 1
        assert self.device["history_labels"][0]["state"] == "present"

    async def test_no_stale_write_when_every_cleanup_races(self):
        self.sample(self.now - 10 * policy.DAY)
        self.runtime._flush_history_block("a")
        execute = self.hass.async_add_executor_job

        async def race(fn, *args):
            result = await execute(fn, *args)
            self.device["label_revision"] = self.device.get("label_revision", 0) + 1
            return result

        self.hass.async_add_executor_job = race
        with self.assertRaisesRegex(ValueError, "changed during trimming"):
            await self.runtime.trim_storage()
        self.runtime.store.async_save.assert_not_awaited()
        assert self.device["history"]

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

    async def test_overnight_skips_paused_devices(self):
        self.device["recording_enabled"] = False
        self.runtime.data["learning_schedule"] = {"enabled": True}
        self.runtime.async_learn = AsyncMock()
        await schedule._run(self.runtime, "2026-09-27")
        self.runtime.async_learn.assert_not_awaited()


def test_untimed_legacy_summaries_expire_without_inventing_original_sample_dates():
    legacy = {"g0_move": {"present": [0] * 20 + [10]}}
    device = {"histograms": legacy}
    updated, _ = cleanup.clean_history(device, [], NOW, 30 * policy.DAY, policy.DEFAULTS)
    assert updated["history_legacy_since"] == NOW
    assert sum(updated["histograms"]["g0_move"]["present"]) == 10
    retained, _ = cleanup.clean_history(
        updated, [], NOW + policy.DAY, 30 * policy.DAY, policy.DEFAULTS
    )
    assert retained["history_legacy_since"] == NOW
    expired, stats = cleanup.clean_history(
        updated, [], NOW + 31 * policy.DAY, 30 * policy.DAY, policy.DEFAULTS
    )
    assert not expired["history_legacy_histograms"]
    assert sum(expired["histograms"]["g0_move"]["present"]) == 0
    assert stats["expired_legacy"] == 1
