"""Dense learning avoids unbounded preparation without changing small-history reports."""

from unittest.mock import patch

import test_tuner as harness
from tuner_under_test.engine.hybrid import adapters, facade, fixed


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
    from types import SimpleNamespace

    view = SimpleNamespace(data={"devices": {"d": {}}, "room_snapshot": None})
    harness.record_samples(view, "d", rows)
    cold = facade.fit(view, "d", ["g0_move"], {"g0_move": 90})
    warm = facade.fit(view, "d", ["g0_move"], {"g0_move": 90})
    assert cold == warm
    assert cold["evidence_budget"]["coverage"] == "all_observed_labelled_time"


def test_medium_history_uses_indexed_quality_without_changing_chronological_validation():
    rows = [
        (
            1000 + i * 6,
            {"g0_move": (40.01 if (i // 400) % 2 else 5.01) + i % 3},
            "present" if (i // 400) % 2 else "not_present",
        )
        for i in range(8000)
    ]
    from types import SimpleNamespace

    view = SimpleNamespace(data={"devices": {"d": {}}, "room_snapshot": None})
    harness.record_samples(view, "d", rows)
    baseline = facade.fit(view, "d", ["g0_move"], {"g0_move": 90})
    indexed = facade.fit(view, "d", ["g0_move"], {"g0_move": 90})
    assert indexed["status"] == "ok"
    assert indexed["adaptive_policy"]["validated"]
    assert indexed == baseline


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
    from tuner_under_test.rooms import attribution

    with patch.object(attribution, "room_targets", side_effect=AssertionError("unused room")):
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
    result = harness.fit(rows, ["g0_move"])
    audit = result["evidence_budget"]
    assert audit["coverage"] == "all_observed_labelled_time"
    assert audit["row_count"] == 4000
    assert audit["scoring_mode"] == "exact"
    assert result["counts"] == {"present": 2000, "not_present": 2000}
    assert result["training"]["duration"]["presence_recall"] == 1
    assert result["training"]["duration"]["false_positive_seconds"] == 0
    assert result["training"]["duration"]["basis"] == "physical_interval_time"


def test_one_scale_timeline_drives_search_preferences_and_reported_duration():
    from types import SimpleNamespace

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
    problem = adapters.device_problem(view, "d", ["g0_move"], {"g0_move": 20}, {}, 0, None, 0, 24)
    with fixed.FixedScores(problem, 1) as scores:
        measured = scores.scores([{"d": {0: 20}}], problem.zones)[0]["d"]
    assert measured["occupied_seconds"] == 12
    assert measured["empty_seconds"] == 12
    assert measured["false_active_seconds"] == 6
    assert measured["occupied_recall"] == 0.5
    # Exactly half does not fail an occupied episode; a trigger fails empty.
    assert measured["mostly_missed_episodes"] == 0
    assert measured["empty_periods_failed"] == 1
    assert measured["cost"] == 400


def test_a_timing_context_that_cannot_fit_cannot_offer_a_recommendation():
    rows = [
        (
            1000 + i * 6,
            {"g0_move": (40 if i < 2000 else 5) + i % 2},
            "present" if i < 2000 else "not_present",
        )
        for i in range(4000)
    ]
    result = harness.fit(
        rows, ["g0_move"], timing={"timeout": 100000, "on_delay": 0, "off_delay": 0}
    )
    assert result["evidence_budget"]["coverage"] == "all_observed_labelled_time"
    assert result["evidence_budget"]["row_count"] == 4000
    assert result["status"] in ("ok", "tradeoff", "unsafe")
    assert result["proposals"]
    assert result["training"]["duration"]["present_seconds"] == 12000
    assert result["training"]["duration"]["empty_seconds"] == 12000


def test_unlabelled_over_budget_history_reports_missing_evidence_not_a_timing_failure():
    rows = [(1000 + i * 6, {"g0_move": 5 + i % 2}, None) for i in range(3000)]
    result = harness.fit(rows, ["g0_move"])
    assert result["status"] == "insufficient"
    assert not result["proposals"]
    assert result["evidence_budget"]["coverage"] == "all_observed_labelled_time"
    assert result["counts"] == {"present": 0, "not_present": 0}
    assert not any("timing context" in warning for warning in result["warnings"])
