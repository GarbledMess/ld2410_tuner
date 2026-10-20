"""Dense learning avoids unbounded preparation without changing small-history reports."""

from unittest.mock import patch

import test_tuner as harness
from tuner_under_test.calibration import event_fitting


def without_fingerprint(value):
    if isinstance(value, dict):
        return {
            key: without_fingerprint(item) for key, item in value.items() if key != "fingerprint"
        }
    if isinstance(value, list):
        return [without_fingerprint(item) for item in value]
    return value


def test_summary_quality_keeps_full_native_report_except_physical_cache_fingerprint():
    rows = [
        (1000 + i * 6, {"g0_move": 40 if i < 60 else 5}, "present" if i < 60 else "not_present")
        for i in range(120)
    ]
    baseline = harness.fit(rows, ["g0_move"])
    with (
        patch.object(event_fitting, "SUMMARY_ROWS", 1, create=True),
        patch.object(
            event_fitting, "collect", side_effect=AssertionError("dense cell quality replay")
        ),
    ):
        dense = harness.fit(rows, ["g0_move"])
    assert without_fingerprint(dense) == without_fingerprint(baseline)


def test_medium_history_uses_indexed_quality_without_changing_chronological_validation():
    rows = [
        (
            1000 + i * 6,
            {"g0_move": (40.01 if (i // 400) % 2 else 5.01) + i % 3},
            "present" if (i // 400) % 2 else "not_present",
        )
        for i in range(8000)
    ]
    with patch.object(event_fitting, "SUMMARY_ROWS", 10**9):
        baseline = harness.fit(rows, ["g0_move"])
    with patch.object(event_fitting, "collect", side_effect=AssertionError("row quality replay")):
        indexed = harness.fit(rows, ["g0_move"])
    assert indexed["status"] == "ok"
    assert indexed["adaptive_policy"]["validated"]
    assert without_fingerprint(indexed) == without_fingerprint(baseline)


def test_whole_percentage_history_preserves_the_complete_fractional_learning_report():
    from math import ceil

    rows = [
        (
            1000 + i * 6,
            {"g0_move": (40.125 if (i // 100) % 2 else 5.125) + i % 3},
            "present" if (i // 100) % 2 else "not_present",
        )
        for i in range(1000)
    ]
    integers = [
        (at, {key: ceil(value) for key, value in values.items()}, label)
        for at, values, label in rows
    ]
    fractional = harness.fit(rows, ["g0_move"])
    whole = harness.fit(integers, ["g0_move"])
    assert whole["status"] == fractional["status"] == "ok"
    assert without_fingerprint(whole) == without_fingerprint(fractional)


def test_fully_own_labelled_dense_learning_does_not_compute_unused_room_attribution():
    rows = [
        (1000 + i * 6, {"g0_move": 40 if i < 60 else 5}, "present" if i < 60 else "not_present")
        for i in range(120)
    ]
    with (
        patch.object(event_fitting, "SUMMARY_ROWS", 1),
        patch.object(
            event_fitting.attribution, "room_targets", side_effect=AssertionError("unused room")
        ),
        patch.object(
            event_fitting.attribution,
            "auto_targets",
            side_effect=AssertionError("unused automatic room"),
        ),
    ):
        result = harness.fit(rows, ["g0_move"])
    assert result["training"]["duration"]["presence_recall"] == 1
    assert result["training"]["duration"]["false_positive_seconds"] == 0


def test_bounded_learning_preserves_class_duration_and_marks_sampled_diagnostics():
    rows = [
        (
            1000 + i * 6,
            {"g0_move": (40 if i < 2000 else 5) + i % 2},
            "present" if i < 2000 else "not_present",
        )
        for i in range(4000)
    ]
    with patch.object(event_fitting, "LEARNING_ROWS", 100, create=True):
        result = harness.fit(rows, ["g0_move"])
    audit = result["evidence_budget"]
    assert audit["status"] == "bounded"
    assert audit["context_rows"] <= audit["allowed_rows"]
    assert audit["context_rows"] < audit["original_rows"]
    assert result["counts"] == {"present": 2000, "not_present": 2000}
    assert result["training"]["duration"]["presence_recall"] == 1
    assert result["training"]["duration"]["false_positive_seconds"] == 0
    assert result["training"]["duration"]["basis"] == "representative_physical_interval_time"
    assert result["evidence_budget"]["episode_counts"] == "original_independent_quality_episodes"


def test_one_scale_timeline_drives_search_preferences_and_reported_duration():
    from types import SimpleNamespace

    from tuner_under_test.calibration import evidence_budget, evidence_plan

    view = SimpleNamespace(data={"devices": {"d": {}}, "room_snapshot": None})
    harness.record_samples(
        view,
        "d",
        [
            (0, {"g0_move": 40}, "present"),
            (6, {"g0_move": 5}, "present"),
            (12, {"g0_move": 40}, "not_present"),
            (18, {"g0_move": 5}, "not_present"),
        ],
    )
    plan = evidence_plan.Plan(
        (
            evidence_budget.Window(0, 6, ("present", "human"), 2),
            evidence_budget.Window(12, 18, ("not_present", "human"), 4),
        ),
        ((0, 24),),
        {"status": "bounded"},
    )
    with patch.object(event_fitting.evidence_plan, "plan", return_value=plan):
        objective = event_fitting._objective(
            view, "d", ["g0_move"], {"g0_move": 20}, {}, 0, None, 0, 24
        )
    measured = event_fitting._measure(objective, {"g0_move": 20})
    assert objective.totals["human", "present"] == measured["duration"]["present_seconds"] == 12
    assert objective.totals["human", "not_present"] == measured["duration"]["empty_seconds"] == 24
    assert measured["duration"]["false_positive_seconds"] == 24
    assert objective.rates({"d": {"g0_move": 20}})["human"] == (0, 1)
    histograms = objective._preference_histograms["g0_move"]
    assert histograms["human", "present"][40] == 12
    assert histograms["human", "not_present"][40] == 24
    assert not histograms["human", "present"][5]
    assert not histograms["human", "not_present"][5]
    assert objective._support("present") == (1, 0)
    # Switching policy must not multiply represented mass again.
    event_fitting._reference(objective, False, 0.5)
    assert objective.totals["human", "present"] == 12
    assert objective._support("present") == (1, 0)


def test_a_timing_context_that_cannot_fit_cannot_offer_a_recommendation():
    rows = [
        (
            1000 + i * 6,
            {"g0_move": (40 if i < 2000 else 5) + i % 2},
            "present" if i < 2000 else "not_present",
        )
        for i in range(4000)
    ]
    with patch.object(event_fitting, "LEARNING_ROWS", 100):
        result = harness.fit(
            rows, ["g0_move"], timing={"timeout": 100000, "on_delay": 0, "off_delay": 0}
        )
    assert result["evidence_budget"]["status"] == "unsupported"
    assert result["status"] == "insufficient"
    assert not result["proposals"]
    assert any("timing context" in warning for warning in result["warnings"])


def test_unlabelled_over_budget_history_reports_missing_evidence_not_a_timing_failure():
    rows = [(1000 + i * 6, {"g0_move": 5 + i % 2}, None) for i in range(3000)]
    with patch.object(event_fitting, "LEARNING_ROWS", 100):
        result = harness.fit(rows, ["g0_move"])
    assert result["status"] == "insufficient"
    assert not result["proposals"]
    assert result["evidence_budget"]["reason"] == "no_scored_evidence"
    assert not any("timing context" in warning for warning in result["warnings"])
