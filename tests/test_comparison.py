"""Fixed-pattern scores share evidence, timing and the learner's error cost."""

import asyncio
import copy
import sys
import unittest
from unittest.mock import patch

import pytest
import test_tuner as harness

comparison = sys.modules["tuner_under_test.calibration.comparison"]
jobs = sys.modules["tuner_under_test.calibration.comparison_jobs"]


def recordings():
    # Gate zero misses every occupant; gate one detects them. OR semantics matter.
    return [(i * 6, {"g0_still": 5, "g1_still": 80}, "present") for i in range(100)] + [
        (1000 + i * 6, {"g0_still": 5, "g1_still": 5}, "not_present") for i in range(100)
    ]


def context():
    def pattern(value, applicable=True):
        return {
            "id": str(value),
            "thresholds": {"g0_still": value, "g1_still": value},
            "applicable": applicable,
        }

    return {
        "keys": ["g0_still", "g1_still"],
        "timing": {"mode": "disabled"},
        "patterns": {
            "live": pattern(4, False),
            "user": pattern(20),
            "current": pattern(100),
            "previous": pattern(20),
            "automatic": pattern(30),
        },
    }


def calculate(rows=None, automatic=None, settings=None):
    with patch.object(
        comparison,
        "collect_samples",
        return_value=(recordings() if rows is None else rows, automatic or []),
    ):
        return comparison.calculate(None, "a", settings or context())


def test_score_boundaries_and_weighting():
    assert comparison.score_from_cost(0) == 100
    assert comparison.score_from_cost(0.000001) == 99.99
    assert comparison.score_from_cost(1000) == 0
    assert comparison.score_from_cost(comparison._error_cost(0.01, 0.01)) == 94
    assert comparison._error_cost(0.01, 0) == comparison._error_cost(0, 0.05)
    for cost in (-1, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            comparison.score_from_cost(cost)


def test_all_patterns_use_same_data_and_live_is_not_saved_current():
    report = calculate()
    scores = report["patterns"]
    assert scores["user"]["score"] == 100
    assert scores["live"]["score"] == 0  # Always active.
    assert scores["current"]["score"] == 0  # Never active.
    assert report["best_slots"] == ["user", "previous", "automatic"]
    assert scores["user"]["human"]["false_negatives"] == 0
    assert scores["user"]["presence_recall"] == 1
    assert scores["user"]["basis"] == "human"


def test_missing_or_incomplete_evidence_is_unscored_not_failure():
    assert calculate(rows=[])["patterns"]["user"]["score"] is None
    rows = recordings()
    rows[0][1].pop("g0_still")
    report = calculate(rows)
    assert report["incomplete_samples"] == 1
    assert report["patterns"]["user"]["score"] == 100


def test_incompatible_saved_result_cannot_be_best_but_can_be_compared():
    settings = context()
    for item in settings["patterns"].values():
        item["applicable"] = False
    settings["patterns"]["previous"] = None
    settings["patterns"]["automatic"]["thresholds"]["g0_still"] = float("nan")
    report = calculate(settings=settings)
    assert report["best_slots"] == []
    assert report["patterns"]["user"]["score"] == 100
    assert report["patterns"]["automatic"]["score"] is None
    assert report["patterns"]["previous"]["reason"] == "No saved result"


def test_auto_only_scores_are_estimates_and_humans_override_conflicting_guesses():
    auto = [
        (ts + 2000, {k: 90 - v for k, v in values.items()}, label, 0.9)
        for ts, values, label in recordings()
    ]
    assert calculate(automatic=auto)["patterns"]["user"]["score"] == 100
    estimates = [(ts, values, label, 0.9) for ts, values, label in recordings()]
    result = calculate(rows=[], automatic=estimates)["patterns"]["user"]
    assert result["score"] == 100
    assert result["basis"] == "estimated"
    assert result["sources"] == {"present": "automatic", "not_present": "automatic"}


def test_current_timing_is_used_for_every_candidate():
    settings = context()
    settings["timing"] = {"timeout": 1, "on_delay": 0.5, "off_delay": 1, "mode": "fallback"}
    with patch.object(comparison, "evaluate", wraps=comparison.evaluate) as evaluate:
        report = calculate(settings=settings)
    assert len(evaluate.call_args_list) == 10
    assert all(call.args[2] == settings["timing"] for call in evaluate.call_args_list)
    assert report["timing"] == settings["timing"]


class ComparisonRuntimeTests(unittest.IsolatedAsyncioTestCase):
    setUp = harness.RuntimeTests.setUp
    asyncTearDown = harness.RuntimeTests.asyncTearDown
    configuration = harness.RuntimeTests.configuration

    async def test_cache_force_live_change_and_no_device_writes(self):
        self.configuration()
        calls = []

        async def execute(fn, *args):
            calls.append(copy.deepcopy(args[-1]))
            return {"patterns": {}, "best_slots": []}

        self.hass.async_add_executor_job = execute
        first = await self.runtime.compare_results("a")
        assert first["state"] == "ready"
        assert await self.runtime.compare_results("a") == {**first, "stale": False}
        assert len(calls) == 1
        await self.runtime.compare_results("a", True)
        assert len(calls) == 2
        self.states["number.radar_g0_move_threshold"].state = "30"
        assert jobs.summary(self.runtime, "a")["state"] == "pending"
        await self.runtime.compare_results("a")
        assert calls[-1]["patterns"]["live"]["thresholds"]["g0_move"] == 30
        self.hass.services.async_call.assert_not_awaited()

    async def test_deduplicates_and_survives_leaving_page(self):
        self.configuration()
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def execute(*args):
            calls.append(args)
            started.set()
            await release.wait()
            return {"patterns": {}}

        self.hass.async_add_executor_job = execute
        request = asyncio.create_task(self.runtime.compare_results("a"))
        await started.wait()
        assert jobs.summary(self.runtime, "a")["state"] == "running"
        second = asyncio.create_task(self.runtime.compare_results("a"))
        await asyncio.sleep(0)
        request.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await request
        release.set()
        assert (await second)["state"] == "ready"
        assert len(calls) == 1

    async def test_labels_changing_mid_comparison_discards_result(self):
        self.configuration()

        async def execute(*args):
            self.device["label_revision"] = 123
            return {"patterns": {}}

        self.hass.async_add_executor_job = execute
        assert await self.runtime.compare_results("a") == {"state": "pending"}
        assert not self.runtime._comparison_cache

    async def test_errors_can_be_retried_and_unknown_devices_do_not_recover(self):
        self.configuration()

        async def execute(*args):
            raise RuntimeError("Cannot decode recording")

        self.hass.async_add_executor_job = execute
        report = await self.runtime.compare_results("a")
        assert report["state"] == "error"
        assert "Cannot decode" in report["reason"]
        assert (await self.runtime.compare_results("a"))["state"] == "error"
        assert jobs.summary(self.runtime, "missing")["state"] == "unavailable"
        with self.assertRaises(ValueError):
            await self.runtime.compare_results("missing")
        self.hass.services.async_call.assert_not_awaited()

    async def test_new_data_refresh_is_throttled(self):
        self.configuration()

        async def execute(*args):
            return {"patterns": {}}

        self.hass.async_add_executor_job = execute
        await self.runtime.compare_results("a")
        self.runtime._history_runtime["a"] = {"samples": [(1, [5] * 18)]}
        assert jobs.summary(self.runtime, "a")["state"] == "ready"
        self.runtime._comparison_cache["a"]["finished_at"] -= 61
        assert jobs.summary(self.runtime, "a")["state"] == "pending"
        assert jobs.summary(self.runtime, "a")["stale"]
