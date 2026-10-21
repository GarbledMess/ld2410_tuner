"""Fixed-pattern scores share evidence, timing and the learner's error cost."""

import asyncio
import copy
import json
import sys
import unittest
from unittest.mock import Mock, patch

import pytest
import test_tuner as harness
from tuner_under_test.worker import calculations, codec, manifest

scoring = sys.modules["tuner_under_test.calibration.scoring"]
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
    """The comparison on a radar whose recorded history is ``rows`` (labelled samples,
    default ``recordings``) and ``automatic`` guessed samples (``harness.record_samples``)."""
    view = harness.types.SimpleNamespace(
        data={"devices": {"a": {}}, "room_snapshot": None}, _history_runtime={}
    )
    harness.record_samples(view, "a", recordings() if rows is None else rows, automatic or [])
    return comparison.calculate(view, "a", settings or context())


def test_score_boundaries_and_weighting():
    assert scoring.score_from_cost(0) == 100
    assert scoring.score_from_cost(0.000001) == 99.99
    assert scoring.score_from_cost(1000) == 0
    assert scoring.score_from_cost(scoring.error_cost(0.01, 0.01)) == 94
    assert scoring.error_cost(0.01, 0) == scoring.error_cost(0, 0.05)
    for cost in (-1, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            scoring.score_from_cost(cost)


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
    fitting = sys.modules["tuner_under_test.calibration.event_fitting"]
    with patch.object(fitting, "_objective", wraps=fitting._objective) as objective:
        report = calculate(settings=settings)
    # Every candidate is measured on the one objective, built with the current timing.
    assert len(objective.call_args_list) == 1
    assert objective.call_args.args[4] == settings["timing"]
    assert report["timing"] == settings["timing"]


def assessment(context):
    return {
        "patterns": {
            slot: {
                "score": 90,
                "ranking": [10],
                "error_cost": 10,
                "thresholds_complete": True,
                "result_id": value.get("id"),
            }
            for slot, value in context["patterns"].items()
        },
        "timing": context["timing"],
        "counts": {},
        "outliers": {},
        "incomplete_samples": 0,
    }


class ComparisonRuntimeTests(unittest.IsolatedAsyncioTestCase):
    setUp = harness.RuntimeTests.setUp
    asyncTearDown = harness.RuntimeTests.asyncTearDown
    configuration = harness.RuntimeTests.configuration

    def scoring_executor(self):
        calls = []

        async def execute(request):
            context = codec.decode(request["parameters"])["context"]
            calls.append(copy.deepcopy(context))
            return assessment(context)

        self.worker_transport.respond = execute
        return calls

    async def test_scores_survive_actual_save_and_runtime_restart(self):
        self.configuration()
        calls = self.scoring_executor()
        self.runtime._schedule_save = Mock()
        first = await self.runtime.compare_results("a")
        assert first["state"] == "ready"
        self.runtime._schedule_save.assert_called_once()
        await self.runtime.async_save()
        persisted = json.loads(json.dumps(self.runtime.store.async_save.call_args.args[0]))
        self.runtime = harness.mod.TunerRuntime(self.hass, self.runtime.store, persisted)
        self.runtime._schedule_save = lambda: None
        assert await self.runtime.compare_results("a") == first
        assert await self.runtime.compare_results("a", True) == first
        assert len(calls) == 1
        self.hass.services.async_call.assert_not_awaited()

    async def test_the_scoring_job_runs_on_an_attribution_prepared_view(self):
        """Regression: unprepared, the job attributed its radar twice (human, then full
        truth) and kept nothing for the next job."""
        self.configuration()
        views = []

        async def execute(request):
            views.append(calculations.restore(manifest.expand(request)["views"]["a"]))
            return assessment(codec.decode(request["parameters"])["context"])

        self.worker_transport.respond = execute
        await self.runtime.compare_results("a")
        # Prepared: its members' guesses are built once for the job (``attribution.prepare``).
        assert getattr(views[0], "_attribution_guesses", None) is not None

    async def test_scoring_frees_the_views_attribution_once_the_evidence_is_collected(self):
        """Regression: the view's guesses and truth readers were freed on the event loop
        after the job."""
        self.configuration()
        from tuner_under_test.rooms import attribution

        view = attribution.prepared_view(self.runtime, "a")
        view._attribution_guesses["a"] = object()
        context, _ = jobs._context(self.runtime, "a")
        comparison.calculate(view, "a", context)
        assert view._attribution_guesses == {}

    async def test_comparison_job_reads_float_threshold_states(self):
        self.configuration()
        for state in self.states.values():
            state.state = f"{float(state.state)}"
        calls = self.scoring_executor()
        report = await self.runtime.compare_results("a")
        assert report["state"] == "ready", report.get("reason")
        live = calls[0]["patterns"]["live"]["thresholds"]
        assert live and all(type(value) is int for value in live.values())
        assert all(
            type(value) is int for value in calls[0]["patterns"]["user"]["thresholds"].values()
        )

    async def test_history_labels_retention_and_timing_do_not_rescore(self):
        self.configuration()
        calls = self.scoring_executor()
        first = await self.runtime.compare_results("a")
        harness.record_rows(self.runtime, "a", [(1, [5] * 18)])
        self.device["label_revision"] = 12
        self.device["training_state"] = "present"
        self.device["training_label_start"] = self.now
        self.runtime.data["storage_settings"] = {"human_days": 40}
        self.runtime.configure_timing(
            {"mode": "disabled", "timeout": 1, "on_delay": 0.5, "off_delay": 1}
        )
        assert jobs.summary(self.runtime, "a") == first
        assert await self.runtime.compare_results("a", True) == first
        assert len(calls) == 1

    async def test_scorer_version_change_recalculates_once(self):
        self.configuration()
        calls = self.scoring_executor()
        await self.runtime.compare_results("a")
        with patch.object(comparison, "SCORER_VERSION", 3):
            assert jobs.summary(self.runtime, "a")["state"] == "pending"
            report = await self.runtime.compare_results("a")
            assert report["scorer_version"] == 3
            assert all(
                item["scorer_version"] == 3 for item in self.device["comparison_scores"].values()
            )
            await self.runtime.compare_results("a")
        assert len(calls) == 2

    async def test_only_new_patterns_are_scored_and_identical_slots_reuse_scores(self):
        self.configuration()
        calls = self.scoring_executor()
        await self.runtime.compare_results("a")
        original = copy.deepcopy(self.device["comparison_scores"])
        saved = self.device["learning_results"]
        saved["automatic"] = copy.deepcopy(saved["user"])
        saved["automatic"]["id"] = "new-nightly-result"
        await self.runtime.compare_results("a")
        assert len(calls) == 1
        for value in saved["automatic"]["proposals"].values():
            value["threshold"] = 30
        await self.runtime.compare_results("a")
        assert list(calls[-1]["patterns"]) == ["automatic"]
        assert all(
            self.device["comparison_scores"][key] == value for key, value in original.items()
        )
        self.states["number.radar_g0_move_threshold"].state = "30"
        await self.runtime.compare_results("a")
        assert list(calls[-1]["patterns"]) == ["live"]
        assert len(self.device["comparison_scores"]) <= 5

    async def test_temporarily_unavailable_live_values_preserve_their_saved_score(self):
        self.configuration()
        calls = self.scoring_executor()
        first = await self.runtime.compare_results("a")
        self.states["number.radar_g0_move_threshold"].state = "unavailable"
        report = await self.runtime.compare_results("a")
        assert report["state"] == "ready"
        assert report["patterns"]["live"]["score"] is None
        self.states["number.radar_g0_move_threshold"].state = "10"
        assert await self.runtime.compare_results("a") == first
        assert len(calls) == 1

    async def test_cache_stays_bounded_and_clear_removes_scores(self):
        self.configuration()
        self.scoring_executor()
        await self.runtime.compare_results("a")
        for value in range(11, 20):
            self.states["number.radar_g0_move_threshold"].state = str(value)
            await self.runtime.compare_results("a")
        assert len(self.device["comparison_scores"]) == 2
        self.runtime.clear_samples("a")
        assert "comparison_scores" not in self.device
        assert jobs.summary(self.runtime, "a")["state"] == "pending"

    async def test_unscored_is_not_polled_repeatedly_but_can_be_retried(self):
        self.configuration()
        calls = []

        async def execute(request):
            calls.append(codec.decode(request["parameters"])["context"])
            report = assessment(codec.decode(request["parameters"])["context"])
            for item in report["patterns"].values():
                item["score"] = None
                item["reason"] = "Not enough evidence"
            return report

        self.worker_transport.respond = execute
        await self.runtime.compare_results("a")
        assert jobs.summary(self.runtime, "a")["state"] == "ready"
        await self.runtime.compare_results("a")
        assert len(calls) == 1
        await self.runtime.compare_results("a", True)
        assert len(calls) == 2

    async def test_deduplicates_and_survives_leaving_page(self):
        self.configuration()
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def execute(request):
            calls.append(codec.decode(request["parameters"])["context"])
            started.set()
            await release.wait()
            return assessment(codec.decode(request["parameters"])["context"])

        self.worker_transport.respond = execute
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

        async def execute(request):
            self.device["label_revision"] = 123
            return assessment(codec.decode(request["parameters"])["context"])

        self.worker_transport.respond = execute
        assert await self.runtime.compare_results("a") == {"state": "pending"}
        assert not self.device.get("comparison_scores")

    async def test_errors_can_be_retried_and_unknown_devices_do_not_recover(self):
        self.configuration()

        async def execute(*args):
            raise RuntimeError("Cannot decode recording")

        self.worker_transport.respond = execute
        report = await self.runtime.compare_results("a")
        assert report["state"] == "error"
        assert "Cannot decode" in report["reason"]
        assert (await self.runtime.compare_results("a"))["state"] == "error"
        assert jobs.summary(self.runtime, "missing")["state"] == "unavailable"
        with self.assertRaises(ValueError):
            await self.runtime.compare_results("missing")
        self.scoring_executor()
        assert (await self.runtime.compare_results("a", True))["state"] == "ready"
        self.hass.services.async_call.assert_not_awaited()
