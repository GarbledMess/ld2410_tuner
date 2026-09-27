"""Synthetic evidence conflicts remain reviewable, never silently relabelled."""

import sys
from copy import deepcopy

import test_tuner  # noqa: F401

review_evidence = sys.modules["tuner_under_test.calibration.diagnostics"].review_evidence


def row(ts, energy, state="not_present"):
    return (ts, {"g2_still": energy, "g3_still": energy}, state)


def test_bursts_count_device_samples_once_and_keep_original_evidence():
    groups = {
        "present": [row(100, 10, "present"), row(106, 60, "present")],
        "not_present": [row(0, 2), row(6, 70), row(12, 80), row(18, 2), row(24, 60)],
    }
    original = deepcopy(groups)
    result = review_evidence(groups, {"g2_still": 40, "g3_still": 50})
    assert groups == original
    assert result["excluded_automatically"] is False
    assert result["period_count"] == 3
    burst = result["periods"][0]
    assert burst == {
        "start": 6,
        "end": 18,
        "state": "not_present",
        "samples": 2,
        "observed_span_seconds": 6,
        "duration_known": False,
        "short_burst": True,
        "gates": {"g2_still": 80, "g3_still": 80},
    }
    assert result["sessions"][0]["errors"] == 3
    assert result["periods"][-1]["gates"] == {}
    assert result["periods"][-1]["short_burst"] is False


def test_missing_time_splits_sessions_and_is_not_an_isolated_spike():
    result = review_evidence(
        {"not_present": [row(0, 0), row(120, 80), row(240, 80), row(246, 0)]}, {"g2_still": 40}
    )
    assert len(result["sessions"]) == 3
    assert result["period_count"] == 2
    assert not any(p["short_burst"] for p in result["periods"])


def test_review_list_is_bounded_and_ranks_longer_errors_first():
    rows = [row(i * 6, 80 if i % 2 else 0) for i in range(100)]
    rows += [row(600 + i * 6, 80) for i in range(10)]
    result = review_evidence({"not_present": rows}, {"g2_still": 40})
    assert result["period_count"] == 50
    assert len(result["periods"]) == 24
    assert result["periods"][0]["samples"] == 11


def test_empty_or_matching_evidence_has_no_review_flags():
    assert review_evidence({}, {})["periods"] == []
    result = review_evidence(
        {"present": [row(0, 60, "present")], "not_present": [row(6, 5)]}, {"g2_still": 40}
    )
    assert result["period_count"] == 0
    assert all(s["errors"] == 0 for s in result["sessions"])


def test_many_empty_bursts_do_not_hide_quiet_presence():
    absent = [row(i * 6, 80 if i % 2 else 0) for i in range(100)]
    present = [row(1000 + i * 6, 0 if i % 2 else 80, "present") for i in range(10)]
    result = review_evidence({"present": present, "not_present": absent}, {"g2_still": 40})
    assert len(result["periods"]) == 24
    assert sum(p["state"] == "present" for p in result["periods"]) == 5
