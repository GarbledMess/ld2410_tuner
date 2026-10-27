"""Native device search replays physical time before intersecting target labels."""

import unittest
from unittest.mock import patch

import pytest
import test_automatic_weight
import test_tuner
from test_event_quality import block
from test_event_quality import view as recorded_view


def view(blocks, labels, runs=()):
    source = recorded_view(blocks, labels, runs)
    source.hass = None
    source.data["room_snapshot"] = None
    return source


def test_device_hold_crosses_the_human_label_boundary():
    from tuner_under_test.calibration.event_fitting import fit

    source = view(
        [block(0, 600, 40), block(600, 1200, 0)],
        [
            {"start": 0, "end": 600, "state": "present"},
            {"start": 600, "end": 1200, "state": "not_present"},
        ],
    )
    result = fit(
        source,
        "any-name",
        ["g0_move"],
        {"g0_move": 20},
        timing={"timeout": 30, "on_delay": 0, "off_delay": 0},
        constraints={"g0_move": {"min": 20, "max": 20, "step": 1}},
    )
    assert result["training"]["duration"]["present_seconds"] == 600
    assert result["training"]["duration"]["false_positive_seconds"] == 30
    assert result["counts"] == {"present": 100, "not_present": 100}
    assert result["proposals"]["g0_move"]["threshold"] == 20


def test_device_query_keeps_physical_on_delay_before_labels():
    from tuner_under_test.calibration.event_fitting import fit

    source = view(
        [block(0, 600, 40), block(600, 1200, 0)],
        [
            {"start": 100, "end": 600, "state": "present"},
            {"start": 600, "end": 1200, "state": "not_present"},
        ],
    )
    result = fit(
        source,
        "any-name",
        ["g0_move"],
        {"g0_move": 20},
        start=100,
        timing={"timeout": 0, "on_delay": 10, "off_delay": 0},
        constraints={"g0_move": {"min": 20, "max": 20, "step": 1}},
    )
    assert result["training"]["duration"]["missed_seconds"] == 0
    assert result["training"]["duration"]["present_seconds"] == 500


def test_full_native_fit_is_invariant_to_duplicate_delivery_density():
    from tuner_under_test.calibration.event_fitting import fit

    labels = [
        {"start": 0, "end": 600, "state": "present"},
        {"start": 600, "end": 1200, "state": "not_present"},
    ]
    sparse = view([block(0, 600, 40), block(600, 1200, 10)], labels)
    dense = view([block(i, i + 10, 40 if i < 600 else 10) for i in range(0, 1200, 10)], labels)
    first = fit(sparse, "any-name", ["g0_move"], {"g0_move": 80})
    second = fit(dense, "any-name", ["g0_move"], {"g0_move": 80})
    assert first["proposals"] == second["proposals"]
    assert first["training"] == second["training"]
    assert first["proposals"]["g0_move"]["threshold"] == 25


def test_native_automatic_only_keeps_raw_admitted_confidence():
    from tuner_under_test.calibration.event_fitting import fit
    from tuner_under_test.history import guesses

    source = view(
        [block(0, 600, 40), block(600, 1200, 10)],
        [],
        [(0, 600, "present", 11, guesses.UNKNOWN), (600, 1200, "not_present", 11, guesses.UNKNOWN)],
    )
    result = fit(source, "any-name", ["g0_move"], {"g0_move": 80}, automatic_weight=0.5)
    assert result["proposals"]["g0_move"]["threshold"] < 40
    assert result["evidence_basis"] == "automatic"
    assert result["automatic_evidence"]["used"]
    assert result["automatic_evidence"]["mean_confidence"]["present"] == pytest.approx(0.4125)


def test_chronology_uses_earlier_native_quality_and_original_held_time():
    from tuner_under_test.calibration.event_fitting import fit

    labels = [
        {
            "start": i * 600,
            "end": (i + 1) * 600,
            "state": "present" if i % 2 == 0 else "not_present",
        }
        for i in range(6)
    ]
    source = view(
        [block(i * 600, (i + 1) * 600, 40 if i % 2 == 0 else 10) for i in range(6)], labels
    )
    result = fit(source, "any-name", ["g0_move"], {"g0_move": 80}, automatic_weight=0.5)
    policy = result["adaptive_policy"]
    assert policy["validated"]
    assert policy["selected"] == "reference"
    assert policy["cutoff"] == 2400
    assert policy["candidates"]["reference"]["score"]["present_seconds"] == 600
    assert policy["candidates"]["reference"]["score"]["empty_seconds"] == 600


def test_native_unknown_exclusion_never_becomes_automatic_fallback():
    from tuner_under_test.calibration.event_fitting import fit
    from tuner_under_test.history import guesses

    source = view(
        [block(0, 1800, 40)],
        [
            {"start": 0, "end": 600, "state": "present"},
            {"start": 600, "end": 1200, "state": "not_present"},
            {"start": 1200, "end": 1800, "state": "unknown"},
        ],
        [(1200, 1800, "present", 20, guesses.UNKNOWN)],
    )
    result = fit(source, "any-name", ["g0_move"], {"g0_move": 20})
    assert result["training"]["duration"]["present_seconds"] == 600
    assert result["estimated_training"]["duration"]["present_seconds"] == 0


def test_reference_native_reports_automatic_offer_separately_from_use():
    from tuner_under_test.calibration.event_fitting import fit
    from tuner_under_test.history import guesses

    labels = [
        {
            "start": i * 600,
            "end": (i + 1) * 600,
            "state": "present" if i % 2 == 0 else "not_present",
        }
        for i in range(6)
    ]
    source = view(
        [block(i * 600, (i + 1) * 600, 40 if i % 2 == 0 else 10) for i in range(7)],
        labels,
        [(3600, 4200, "present", 20, guesses.UNKNOWN)],
    )
    result = fit(source, "any-name", ["g0_move"], {"g0_move": 80}, automatic_weight=0.5)
    assert result["adaptive_policy"]["selected"] == "reference"
    assert not result["automatic_evidence"]["used"]
    assert result["automatic_evidence"]["offered_samples"]["present"] == 100
    assert result["estimated_training"]["duration"]["present_seconds"] == 0


class NativeDeviceRuntimeTests(unittest.IsolatedAsyncioTestCase):
    setUp = test_automatic_weight.DeviceLearnTests.setUp
    asyncTearDown = test_automatic_weight.DeviceLearnTests.asyncTearDown
    configuration = test_automatic_weight.DeviceLearnTests.configuration

    @patch("time.time", return_value=2_000_000_000)
    def test_runtime_dispatches_native_history_before_class_splitting(self, _clock):
        self.now = 2_000_000_000
        self.configuration()
        start = self.now - 1200
        for events, end in (block(start, start + 600, 40), block(start + 600, self.now, 0)):
            test_tuner.recorded(self.runtime, "a", events, end)
        self.device["history_labels"] = [
            {"start": start, "end": start + 600, "state": "present"},
            {"start": start + 600, "end": self.now, "state": "not_present"},
        ]
        self.runtime._fit_timing = {"timeout": 30, "on_delay": 0, "off_delay": 0}
        result = self.runtime._fit_history(
            "a", {"g0_move": "number.radar_g0_move_threshold"}, {"g0_move": 20}
        )
        assert result["training"]["duration"]["false_positive_seconds"] == 30
        assert result["threshold_constraints"]["g0_move"]["step"] == 1
        assert result["timing"]["model"] == "physical_interval_time"


def test_one_parent_annotation_is_not_two_missed_episodes_across_outage():
    from tuner_under_test.calibration.event_fitting import fit

    source = view(
        [block(0, 600, 40), block(1200, 1800, 40), block(1800, 2400, 0)],
        [
            {"start": 0, "end": 1800, "state": "present"},
            {"start": 1800, "end": 2400, "state": "not_present"},
        ],
    )
    result = fit(
        source,
        "any-name",
        ["g0_move"],
        {"g0_move": 100},
        constraints={"g0_move": {"min": 100, "max": 100, "step": 1}},
    )
    assert result["training"]["duration"]["present_seconds"] == 1200
    assert result["training"]["duration"]["missed_presence_episodes"] == 1
    assert result["training"]["duration"]["presence_episodes"] == 1


def test_always_active_native_empty_is_unsafe():
    from tuner_under_test.calibration.event_fitting import fit

    source = view(
        [block(0, 1200, 40)],
        [
            {"start": 0, "end": 600, "state": "present"},
            {"start": 600, "end": 1200, "state": "not_present"},
        ],
    )
    result = fit(
        source,
        "any-name",
        ["g0_move"],
        {"g0_move": 20},
        constraints={"g0_move": {"min": 20, "max": 20, "step": 1}},
    )
    assert result["status"] == "unsafe"


def test_native_search_cost_really_trades_elapsed_presence_against_false_time():
    from tuner_under_test.calibration.event_fitting import fit

    source = view(
        [block(0, 500, 5), block(500, 600, 40), block(600, 1200, 10)],
        [
            {"start": 0, "end": 600, "state": "present"},
            {"start": 600, "end": 1200, "state": "not_present"},
        ],
    )
    result = fit(source, "any-name", ["g0_move"], {"g0_move": 80})
    assert result["proposals"]["g0_move"]["threshold"] < 5
    assert result["training"]["duration"]["missed_seconds"] == 0


def test_independent_room_empty_and_attributed_present_keep_device_precedence():
    from test_event_attribution import fixture
    from tuner_under_test.calibration.event_fitting import fit

    source, saved = fixture()
    # This regression exercises inferred multi-radar evidence; singleton truth is direct.
    saved["groups"]["zone"]["device_ids"].append("other")
    saved["members"]["other"] = {}
    source.hass = None
    source.data["room_snapshot"] = saved
    result = fit(source, "any-name", ["g0_move"], {"g0_move": 80}, automatic_weight=0.5)
    assert result["counts"] == {"present": 0, "not_present": 100}
    assert result["automatic_evidence"]["samples"]["present"] == 100
    assert result["automatic_evidence"]["mean_confidence"]["present"] == 0.75
    assert result["evidence_basis"] == "mixed"
    source.data["devices"]["any-name"]["history_labels"] = [
        {"start": 0, "end": 600, "state": "unknown"}
    ]
    blocked = fit(source, "any-name", ["g0_move"], {"g0_move": 80}, automatic_weight=0.5)
    assert blocked["status"] == "insufficient"
    assert blocked["automatic_evidence"]["samples"]["present"] == 0


def test_future_native_energy_cannot_change_earlier_models_or_candidates():
    from tuner_under_test.calibration.event_fitting import fit

    labels = [
        {
            "start": i * 600,
            "end": (i + 1) * 600,
            "state": "present" if i % 2 == 0 else "not_present",
        }
        for i in range(6)
    ]
    blocks = [block(i * 600, (i + 1) * 600, 40 if i % 2 == 0 else 10) for i in range(6)]
    before = fit(
        view(blocks, labels), "any-name", ["g0_move"], {"g0_move": 80}, automatic_weight=0.5
    )
    after = fit(
        view(blocks[:4] + [block(2400, 3000, 2), block(3000, 3600, 99)], labels),
        "any-name",
        ["g0_move"],
        {"g0_move": 80},
        automatic_weight=0.5,
    )
    assert before["adaptive_policy"]["training_model"] == after["adaptive_policy"]["training_model"]
    assert after["validation"]["duration"]["missed_seconds"] == 600
    for policy in ("reference", "corrected_positive"):
        assert (
            before["adaptive_policy"]["candidates"][policy]["thresholds"]
            == after["adaptive_policy"]["candidates"][policy]["thresholds"]
        )


def test_native_support_is_duration_and_raw_float_boundary_is_exact():
    from tuner_under_test.calibration.event_fitting import fit

    labels = [
        {"start": 0, "end": 600, "state": "present"},
        {"start": 600, "end": 1200, "state": "not_present"},
    ]
    result = fit(
        view([block(0, 600, 20.1), block(600, 1200, 19.9)], labels),
        "any-name",
        ["g0_move"],
        {"g0_move": 80},
    )
    assert result["proposals"]["g0_move"]["threshold"] == 20
    assert result["training"]["duration"]["missed_seconds"] == 0
    assert result["training"]["duration"]["false_positive_seconds"] == 0
    tiny = fit(
        view(
            [block(0, 1, 20), block(1, 2, 0)],
            [
                {"start": 0, "end": 1, "state": "present"},
                {"start": 1, "end": 2, "state": "not_present"},
            ],
        ),
        "any-name",
        ["g0_move"],
        {"g0_move": 80},
    )
    assert tiny["status"] == "insufficient"
    assert tiny["proposals"] == {}


def test_native_saved_result_keeps_duration_and_validation_adapter_contract():
    from tuner_under_test.calibration.event_fitting import fit

    labels = [
        {
            "start": i * 600,
            "end": (i + 1) * 600,
            "state": "present" if i % 2 == 0 else "not_present",
        }
        for i in range(6)
    ]
    result = fit(
        view([block(i * 600, (i + 1) * 600, 40 if i % 2 == 0 else 10) for i in range(6)], labels),
        "any-name",
        ["g0_move"],
        {"g0_move": 80},
        automatic_weight=0.5,
    )
    assert result["validation"]["duration"]["present_seconds"] == 600
    assert result["current_validation"]["duration"]["missed_seconds"] == 600
    assert result["training"]["duration"]["longest_missed_seconds"] == 0
    assert result["training"]["duration"]["missed_time_cost"] == 5
    # Recent training is the newest 20% of each human class: 360 of the 1800 present seconds.
    assert result["recent_training"]["duration"]["present_seconds"] == 360


def test_native_fixed_pattern_assessment_never_refits_or_loses_hold():
    from tuner_under_test.calibration.event_fitting import assess

    source = view(
        [block(0, 600, 40), block(600, 1200, 0)],
        [
            {"start": 0, "end": 600, "state": "present"},
            {"start": 600, "end": 1200, "state": "not_present"},
        ],
    )
    result = assess(
        source, "any-name", {"g0_move": 20}, {"timeout": 30, "on_delay": 0, "off_delay": 0}
    )
    assert result["human"]["duration"]["false_positive_seconds"] == 30
    assert result["quality"]["provenance"] == "recorded_basis_only"


def test_native_production_comparison_scores_fixed_patterns_on_same_physical_trace():
    from tuner_under_test.calibration.comparison import calculate
    from tuner_under_test.calibration.quality import QUALITY_VERSION

    source = view(
        [block(0, 600, 40), block(600, 1200, 0)],
        [
            {"start": 0, "end": 600, "state": "present"},
            {"start": 600, "end": 1200, "state": "not_present"},
        ],
    )
    context = {
        "keys": ["g0_move"],
        "quality_version": QUALITY_VERSION,
        "timing": {"timeout": 30, "on_delay": 0, "off_delay": 0},
        "patterns": {"user": {"id": "a", "applicable": True, "thresholds": {"g0_move": 20}}},
    }
    report = calculate(source, "any-name", context)
    assert report["patterns"]["user"]["score"] == 95
    assert report["patterns"]["user"]["human"]["duration"]["false_positive_seconds"] == 30
    assert report["counts"]["human"] == {"present": 100, "not_present": 100}


def test_native_raw_diagnostic_and_corrected_assessment_share_admission_and_weighted_cost():
    from tuner_under_test.calibration.event_fitting import assess
    from tuner_under_test.history import guesses

    source = view(
        [block(0, 600, 40), block(600, 1800, 0)],
        [],
        [
            (0, 600, "present", 11, guesses.UNKNOWN),
            (600, 1200, "present", 20, guesses.UNKNOWN),
            (1200, 1800, "not_present", 20, guesses.UNKNOWN),
        ],
    )
    corrected = assess(source, "any-name", {"g0_move": 20})
    raw = assess(source, "any-name", {"g0_move": 20}, quality_context=False)
    assert corrected["automatic"]["present_samples"] == raw["automatic"]["present_samples"] == 200
    assert corrected["automatic_evidence"]["mean_confidence"]["present"] == pytest.approx(
        (0.4125 + 0.75) / 2
    )
    assert raw["automatic_evidence"]["mean_confidence"]["present"] == pytest.approx((0.55 + 1) / 2)
    assert raw["quality"] is None
    assert corrected["automatic_rank"][1] == pytest.approx(0.75 / (0.4125 + 0.75))


def test_native_comparison_automatic_primary_uses_confidence_weighted_duration():
    from tuner_under_test.calibration.comparison import calculate
    from tuner_under_test.calibration.quality import QUALITY_VERSION
    from tuner_under_test.history import guesses

    source = view(
        [block(0, 600, 40), block(600, 1800, 0)],
        [],
        [
            (0, 600, "present", 11, guesses.UNKNOWN),
            (600, 1200, "present", 20, guesses.UNKNOWN),
            (1200, 1800, "not_present", 20, guesses.UNKNOWN),
        ],
    )
    report = calculate(
        source,
        "any-name",
        {
            "keys": ["g0_move"],
            "timing": {},
            "quality_version": QUALITY_VERSION,
            "patterns": {"user": {"id": "x", "applicable": True, "thresholds": {"g0_move": 20}}},
        },
    )
    assert report["patterns"]["user"]["presence_recall"] == pytest.approx(0.4125 / (0.4125 + 0.75))


def test_gate_preferences_weigh_readings_by_how_long_they_held():
    """Regression: native rows are change spans, so counting rows let three brief loud
    readings outweigh five minutes of quiet presence; each row weighs its held time."""
    from tuner_under_test.calibration.separation import gate_preference

    quiet = (0, {"g0_move": 30}, "present", 1.0, 50.0)  # 300 s held, in six-second units.
    loud = [(300 + i, {"g0_move": 80}, "present", 1.0, 1 / 6) for i in range(3)]
    empty = [
        (400, {"g0_move": 5}, "not_present", 1.0, 50.0),
        (700, {"g0_move": 60}, "not_present", 1.0, 0.5),
    ]
    groups = {"present": [quiet, *loud], "not_present": empty}
    found = gate_preference("g0_move", groups, {"present": [], "not_present": []})
    assert found["presence_reference"] == 30
    # Unit weights (the samples of a sampled recording) keep the count rule exactly.
    rows = [(i, {"g0_move": value}, "present", 1.0, 1) for i, value in enumerate([30, 80, 80, 80])]
    counted = gate_preference("g0_move", {"present": rows, "not_present": []}, groups)
    assert counted["presence_reference"] == 80
