"""Independent scoring and saved-report compatibility contracts."""

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from hybrid_referee import referee_score
from hybrid_scenarios import assert_device_contract, assert_room_contract, synthetic_device_report


def test_referee_distinguishes_triggered_failed_and_mostly_active_empty():
    truth = [(0, 10, "present", "human", 1), (10, 20, "not_present", "human", 1)]
    for stop, cost, failed in [(20, 200, 1), (15, 50, 0)]:
        radar = SimpleNamespace(
            times=[0, stop], ends=[stop, 20], columns=[bytes([90, 0])] * 18, timing={}
        )
        result = referee_score({"a": radar}, {"a": {0: 40}}, truth, (0, 20))
        assert result["cost"] == cost
        assert result["false_triggers"] == 0
        assert result["empty_periods_with_false_trigger"] == 0
        assert result["empty_periods_mostly_false_active"] == failed
        assert result["empty_periods_failed"] == failed


def test_report_contract_rejects_missing_duration_field():
    report = synthetic_device_report()
    assert_device_contract(report)
    broken = deepcopy(report)
    del broken["training"]["duration"]["longest_missed_seconds"]
    with pytest.raises(AssertionError, match="longest_missed_seconds"):
        assert_device_contract(broken)


def test_existing_synthetic_saved_reports_keep_the_contract():
    root = Path(__file__).parent / "fixtures" / "characterisation"
    assert_device_contract(json.loads((root / "device_fit.json").read_text()))
    assert_room_contract(json.loads((root / "room_assessment_two_members.json").read_text()))
