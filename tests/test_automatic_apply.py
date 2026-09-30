"""Overnight writes require a fresh improvement on common evidence, never cached scores."""

import asyncio
import sys
import unittest
from copy import deepcopy
from unittest.mock import AsyncMock, patch

import pytest
import test_tuner as harness
from test_comparison import calculate, context

automatic = sys.modules["tuner_under_test.calibration.automatic"]
schedule = sys.modules["tuner_under_test.runtime.schedule"]
results = sys.modules["tuner_under_test.calibration.results"]


def test_decisions_use_unrounded_weighted_cost_and_require_both_states():
    patterns = calculate()["patterns"]
    assert automatic.decision(patterns) is None
    patterns["live"] = deepcopy(patterns["automatic"])
    assert "do not reduce" in automatic.decision(patterns)
    patterns["automatic"]["error_cost"] = 0.001
    assert "do not reduce" in automatic.decision(patterns)
    patterns["live"]["error_cost"] = 0.002
    assert automatic.decision(patterns) is None  # Rounded display score need not differ.
    patterns["automatic"]["applicable"] = False
    assert "no longer match" in automatic.decision(patterns)
    assert "Not enough" in automatic.decision(calculate(rows=[])["patterns"])


def test_missed_presence_cost_outweighs_small_false_positive_saving():
    settings = context()
    # Raise the threshold to suppress noise but lose much more quiet presence.
    settings["patterns"]["automatic"]["thresholds"] = dict.fromkeys(settings["keys"], 60)
    settings["patterns"]["live"]["thresholds"] = dict.fromkeys(settings["keys"], 10)
    rows = [(i * 6, dict.fromkeys(settings["keys"], 50), "present") for i in range(100)]
    rows += [
        (1000 + i * 6, dict.fromkeys(settings["keys"], 20 if i < 10 else 5), "not_present")
        for i in range(100)
    ]
    assert "do not reduce" in automatic.decision(
        calculate(rows=rows, settings=settings)["patterns"]
    )


class AutomaticApplyTests(unittest.IsolatedAsyncioTestCase):
    asyncTearDown = harness.RuntimeTests.asyncTearDown
    configuration = harness.RuntimeTests.configuration

    def setUp(self):
        harness.RuntimeTests.setUp(self)
        self.configuration()
        self.runtime.configure_learning_schedule(True, "03:00", True)
        self.runtime.configure_timing(
            {"mode": "disabled", "timeout": 1, "on_delay": 0.5, "off_delay": 1}
        )
        self.learned = results.remember_learning(
            self.device, self.device["last_learning"], "automatic"
        )
        self.keys = list(self.learned["entities"])
        rows = [(i * 6, dict.fromkeys(self.keys, 80), "present") for i in range(100)]
        rows += [(1000 + i * 6, dict.fromkeys(self.keys, 15), "not_present") for i in range(100)]
        self.evidence = patch.object(
            automatic.comparison, "collect_samples", return_value=(rows, [])
        )
        self.evidence.start()
        self.addCleanup(self.evidence.stop)
        self.report = {}

    async def run_apply(self):
        await automatic.run(self.runtime, "a", self.learned, self.report)
        return self.report

    async def test_improvement_applies_via_writer_and_retains_previous_with_fresh_evidence(self):
        self.device["comparison_scores"] = {"stale": {"score": 100, "scorer_version": 1}}
        await self.run_apply()
        assert self.report["status"] == "applied"
        assert self.report["assessment"]["patterns"]["live"]["score"] == 0
        assert self.report["assessment"]["patterns"]["automatic"]["score"] == 100
        assert self.hass.services.async_call.await_count == 6
        saved = self.device["learning_results"]
        assert saved["current"]["id"] == self.learned["id"]
        assert all(p["threshold"] == 10 for p in saved["previous"]["proposals"].values())
        assert self.report["scorer_version"] == automatic.comparison.SCORER_VERSION
        # A second night with identical live settings does not churn gates or Previous.
        previous = deepcopy(saved["previous"])
        await self.run_apply()
        assert self.report["status"] == "skipped"
        assert self.hass.services.async_call.await_count == 6
        assert saved["previous"] == previous

    async def test_disabled_policy_or_paused_recording_prevents_writes(self):
        for flag in (False, True):
            self.runtime.configure_learning_schedule(True, "03:00", flag)
            self.device["recording_enabled"] = False
            await self.run_apply()
            assert self.report["status"] == "skipped"
        self.hass.services.async_call.assert_not_awaited()

    async def test_labels_thresholds_timing_or_policy_changing_during_compare_prevent_writes(self):
        async def execute(fn, *args):
            measured = fn(*args)
            self.change()
            return measured

        self.hass.async_add_executor_job = execute
        changes = [
            lambda: self.device.update(label_revision=1),
            lambda: setattr(self.states["number.radar_g0_move_threshold"], "state", "11"),
            lambda: self.runtime.data["timing_settings"].update(mode="fallback"),
            lambda: self.runtime.data["learning_schedule"].update(auto_apply=False),
            lambda: self.device["learning_results"]["automatic"].update(id="replaced"),
        ]
        for change in changes:
            original_data = deepcopy(self.runtime.data)
            self.change = change
            await self.run_apply()
            assert self.report["status"] == "skipped"
            self.runtime.data = original_data
            self.device = self.runtime.data["devices"]["a"]
            self.learned = deepcopy(self.device["learning_results"]["automatic"])
            self.states["number.radar_g0_move_threshold"].state = "10"
        self.hass.services.async_call.assert_not_awaited()

    async def test_change_during_device_preparation_is_checked_again(self):
        async def prepare(*args):
            self.device["label_revision"] = 9

        with patch.object(automatic.device_io, "prepare_device", prepare):
            await self.run_apply()
        assert self.report["status"] == "skipped"
        self.hass.services.async_call.assert_not_awaited()

    async def test_mid_write_change_stops_remaining_writes_and_does_not_promote(self):
        async def write(*args, **kwargs):
            await self.report_write(*args, **kwargs)
            self.runtime.data["learning_schedule"]["auto_apply"] = False

        self.hass.services.async_call.side_effect = write
        await self.run_apply()
        assert self.report["status"] == "partial"
        assert self.hass.services.async_call.await_count == 1
        assert "current" not in self.device["learning_results"]
        assert len(self.report["writes"]["skipped"]) == 5

    async def test_last_write_state_change_is_not_reported_as_success(self):
        async def write(*args, **kwargs):
            await self.report_write(*args, **kwargs)
            if self.hass.services.async_call.await_count == len(self.keys):
                self.device["label_revision"] = 99

        self.hass.services.async_call.side_effect = write
        await self.run_apply()
        assert self.report["status"] == "partial"
        assert "configuration" in self.report["writes"]["skipped"]
        assert "current" not in self.device["learning_results"]

    async def test_optional_package_and_fallback_timing_are_supported(self):
        from test_timing import entity, state

        cases = [
            ("device", {}),  # Ordinary ESPHome without timing metadata remains supported.
            ("fallback", {}),
            (
                "device",
                {
                    "number.radar_timeout": state(1),
                    "sensor.radar_presence_on_delay": state("500ms"),
                    "sensor.radar_presence_off_delay": state("1s"),
                },
            ),
        ]
        for mode, controls in cases:
            self.configuration()
            self.runtime.data["timing_settings"]["mode"] = mode
            self.states.update(controls)
            self.registry.entities.update({key: entity(key) for key in controls})
            await self.run_apply()
            assert self.report["status"] == "applied"
            if controls:
                timing = self.report["assessment"]["timing"]
                assert timing["scope"] == "reported_presence"
                assert set(timing["sources"].values()) == {"device"}

    async def test_automatic_only_evidence_is_allowed_and_identified_as_estimated(self):
        rows = [(i * 6, dict.fromkeys(self.keys, 80), "present", 0.9) for i in range(100)]
        rows += [
            (1000 + i * 6, dict.fromkeys(self.keys, 15), "not_present", 0.9) for i in range(100)
        ]
        with patch.object(automatic.comparison, "collect_samples", return_value=([], rows)):
            await self.run_apply()
        assert self.report["status"] == "applied"
        assert self.report["assessment"]["patterns"]["automatic"]["basis"] == "estimated"

    async def test_failed_write_is_reported_without_losing_learned_result(self):
        self.hass.services.async_call.side_effect = RuntimeError("offline")
        await self.run_apply()
        assert self.report["status"] == "partial"
        assert "offline" in str(self.report["writes"]["skipped"])
        assert self.device["learning_results"]["automatic"]["id"] == self.learned["id"]
        assert "current" not in self.device["learning_results"]

    async def test_missing_evidence_or_replaced_result_does_not_apply(self):
        with patch.object(automatic.comparison, "collect_samples", return_value=([], [])):
            await self.run_apply()
        assert "Not enough" in self.report["reason"]
        self.learned = {**self.learned, "id": "stale"}
        await self.run_apply()
        assert "replaced" in self.report["reason"]
        self.hass.services.async_call.assert_not_awaited()

    async def test_comparison_errors_and_cancellation_remain_visible(self):
        for error, status in (
            (RuntimeError("decode failed"), "error"),
            (asyncio.CancelledError(), "interrupted"),
        ):
            self.hass.async_add_executor_job = AsyncMock(side_effect=error)
            if status == "interrupted":
                with pytest.raises(asyncio.CancelledError):
                    await self.run_apply()
            else:
                await self.run_apply()
            assert self.report["status"] == status
            assert not self.runtime._applying
        self.hass.services.async_call.assert_not_awaited()

    async def test_overnight_applies_but_manual_learning_does_not(self):
        self.runtime._learn_once = AsyncMock(return_value=deepcopy(self.learned))
        await self.runtime.async_learn("a")
        self.hass.services.async_call.assert_not_awaited()
        await schedule._learn_device(self.runtime, "a", self.device, "2026-09-30")
        assert self.device["nightly_learning"]["automatic_apply"]["status"] == "applied"
        assert self.device["nightly_learning"]["status"] == "ok"
        await self.runtime.async_save()
        assert (
            self.runtime.store.async_save.call_args.args[0]["devices"]["a"]["nightly_learning"][
                "automatic_apply"
            ]["status"]
            == "applied"
        )

    def test_policy_validation_and_restart_during_apply(self):
        with pytest.raises(ValueError):
            self.runtime.configure_learning_schedule(True, "03:00", "yes")
        self.device["nightly_learning"] = {
            "status": "running",
            "automatic_apply": {"status": "applying"},
        }
        schedule.restore(self.runtime)
        assert self.device["nightly_learning"]["automatic_apply"]["status"] == "interrupted"
