"""Saved comparisons follow the learner quality policy across restarts."""

import unittest
from copy import deepcopy

import test_comparison
import test_tuner  # noqa: F401
from tuner_under_test.calibration import comparison_cache, comparison_jobs, settings
from tuner_under_test.calibration.constants import METHOD
from tuner_under_test.calibration.quality import QUALITY_VERSION


def test_default_signature_always_names_weight_quality_and_method():
    assert settings.signature({}) == {
        "automatic_weight": 0.0,
        "quality_version": QUALITY_VERSION,
        "learner_method": METHOD,
    }


def test_cache_policy_change_invalidates_identical_thresholds_and_keeps_quality_audit():
    pattern = {"id": "a", "thresholds": {"g0_move": 20}, "applicable": True}
    context = {
        "entities": {"g0_move": "number.g"},
        "keys": ["g0_move"],
        "patterns": {"user": pattern},
        **settings.signature({}),
    }
    report = {
        "patterns": {"user": {"score": 100, "ranking": [0], "thresholds_complete": True}},
        "quality": {"fingerprint": "evidence", "quality_version": QUALITY_VERSION},
    }
    device = {}
    comparison_cache.remember(device, context, report)
    record = comparison_cache.stored(device, context, pattern)
    assert record["quality"] == report["quality"]
    assert record["quality_version"] == QUALITY_VERSION
    for key, value in (
        ("quality_version", QUALITY_VERSION + 1),
        ("automatic_weight", 0.5),
        ("learner_method", "next"),
    ):
        changed = {**context, key: value}
        assert comparison_cache.stored(device, changed, pattern) is None
    assert comparison_cache.stored(device, deepcopy(context), pattern) == record


class ComparisonPolicyRuntimeTests(unittest.IsolatedAsyncioTestCase):
    setUp = test_comparison.ComparisonRuntimeTests.setUp
    asyncTearDown = test_comparison.ComparisonRuntimeTests.asyncTearDown
    configuration = test_comparison.ComparisonRuntimeTests.configuration
    scoring_executor = test_comparison.ComparisonRuntimeTests.scoring_executor

    async def test_production_job_passes_policy_and_reuses_only_unchanged_policy(self):
        self.configuration()
        calls = self.scoring_executor()
        await self.runtime.compare_results("a")
        assert calls[0]["quality_version"] == QUALITY_VERSION
        assert calls[0]["automatic_weight"] == 0
        assert calls[0]["learner_method"] == METHOD
        await self.runtime.compare_results("a")
        assert len(calls) == 1
        self.runtime.data.setdefault("learning_schedule", {})["automatic_weight"] = 0.5
        assert comparison_jobs.summary(self.runtime, "a")["state"] == "pending"
        await self.runtime.compare_results("a")
        assert len(calls) == 2


def test_cache_rejects_records_missing_or_claiming_another_policy_even_at_matching_key():
    pattern = {"thresholds": {"g0_move": 20}, "applicable": True}
    context = {
        "entities": {"g0_move": "number.g"},
        "keys": ["g0_move"],
        "patterns": {"user": pattern},
        **settings.signature({}),
    }
    device = {}
    comparison_cache.remember(device, context, {"patterns": {"user": {"score": 90}}})
    key = comparison_cache.pattern_key(context, pattern)
    device[comparison_cache.FIELD][key].pop("learner_policy")
    assert comparison_cache.stored(device, context, pattern) is None
    device[comparison_cache.FIELD][key]["learner_policy"] = {
        **settings.signature({}),
        "quality_version": -1,
    }
    assert comparison_cache.stored(device, context, pattern) is None
