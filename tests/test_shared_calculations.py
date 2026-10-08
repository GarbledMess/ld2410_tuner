"""Shared mechanics preserve boundary, scoring and background-task contracts."""

from unittest.mock import Mock

import pytest
import test_tuner as harness  # noqa: F401 - install the Home Assistant test harness
from tuner_under_test.calibration.duration import _confidence_time
from tuner_under_test.calibration.intervals import WeightedTime
from tuner_under_test.calibration.metrics import metrics
from tuner_under_test.calibration.scoring import improves, score_from_cost
from tuner_under_test.calibration.timing_metrics import evaluate
from tuner_under_test.runtime.tasks import finish_task


def test_exact_labels_and_telemetry_keep_different_boundaries():
    labels = WeightedTime([0, 10, 20], [1, 0, 1])
    telemetry = _confidence_time(
        [(0, {}, "present", 1), (10, {}, "present", 0), (20, {}, "present", 1)]
    )
    assert labels.between(5, 10) == 5
    assert telemetry.between(5, 10) == 0
    assert labels.between(10, 20) == 0
    assert telemetry.between(10, 20) == 5


def test_confidence_integral_is_invariant_to_subdivision():
    coarse = WeightedTime([0, 10, 20], [0.7, 0.2, 0])
    dense = WeightedTime([0, 3, 8, 10, 13, 20], [0.7, 0.7, 0.7, 0.2, 0.2, 0])
    for start, end in ((0, 20), (3, 15), (10, 10), (10, 20)):
        assert coarse.between(start, end) == pytest.approx(dense.between(start, end))


def test_raw_and_untimed_outcomes_agree_on_gaps_and_strict_thresholds():
    rows = [
        (30, {"g0_still": 20}, "not_present"),
        (6, {"g0_still": 21}, "present"),
        (0, {"g0_still": 20}, "present"),
        (36, {"g0_still": 21}, "not_present"),
    ]
    groups = {
        label: sorted(row for row in rows if row[2] == label)
        for label in ("present", "not_present")
    }
    raw = metrics(rows, {"g0_still": 20})
    timed = evaluate(groups, {"g0_still": 20})
    assert raw == {key: timed[key] for key in raw}
    assert raw["false_negatives"] == raw["false_positives"] == 1
    assert raw["presence_episodes"] == raw["false_trigger_bursts"] == 1


def test_improvement_uses_unrounded_cost_and_rejects_numerical_ties():
    current = {"error_cost": 6.00004}
    candidate = {"error_cost": 6.00003}
    assert score_from_cost(current["error_cost"]) == score_from_cost(candidate["error_cost"])
    assert improves(current, candidate)
    assert not improves(current, {"error_cost": current["error_cost"] - 1e-10})
    assert not improves(current, current)
    assert not improves(candidate, current)


@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("replaced", [False, True])
def test_finishing_task_preserves_replacement_and_handles_unobserved_failure(cancelled, replaced):
    task = Mock()
    task.cancelled.return_value = cancelled
    task.exception.return_value = ValueError("Job failed after the browser left")
    replacement = object()
    tasks = {"radar": replacement if replaced else task}
    finish_task(tasks, "radar", task)
    assert tasks == ({"radar": replacement} if replaced else {})
    assert task.exception.call_count == (0 if cancelled else 1)
