"""Synthetic report-boundary tests; no search implementation or private history."""

from array import array
from copy import deepcopy
from dataclasses import replace

import pytest
import test_tuner  # noqa: F401
from hybrid_scenarios import assert_device_contract, assert_room_contract
from tuner_under_test.engine.hybrid.types import (
    EvidencePolicy,
    FitProblem,
    FitResult,
    RadarSpec,
    RowBlock,
    ScorePlan,
    TruthSpan,
    ZoneSpec,
)


class Rows:
    def __init__(self, pieces, split=1):
        self.pieces, self.split = pieces, split

    def blocks(self, lo, hi):
        for a, b, energy in self.pieces:
            for i in range(self.split):
                x, y = (
                    max(lo, a + (b - a) * i / self.split),
                    min(hi, a + (b - a) * (i + 1) / self.split),
                )
                if y > x:
                    yield RowBlock(
                        array("d", [x]), array("d", [y]), (bytes([energy]),) + (b"\xff",) * 17
                    )

    def rows_in(self, lo, hi):
        return sum(1 for _ in self.blocks(lo, hi))


def problem(split=1):
    spans = tuple(
        TruthSpan(a, b, s, "human", 1, str(a), ())
        for a, b, s in (
            (0, 60, "present"),
            (60, 120, "not_present"),
            (120, 180, "present"),
            (180, 240, "not_present"),
        )
    )
    return FitProblem(
        {"r": RadarSpec("r", (0,), {0: tuple(range(101))}, {0: 50}, {})},
        (ZoneSpec("z", ("r",), spans),),
        0,
        240,
        None,
        {"r": Rows([(0, 60, 70), (60, 120, 10), (120, 180, 20), (180, 240, 60)], split)},
        EvidencePolicy(0, False, {}),
    )


def metrics():
    return {
        "cost": 410.25,
        "score": 0,
        "time_term": 2.5,
        "episode_term": 1.6025,
        "occupied_seconds": 120,
        "empty_seconds": 120,
        "occupied_recall": 0.6,
        "false_active_seconds": 30,
        "occupied_episodes": 2,
        "empty_periods": 2,
        "completely_missed_episodes": 0,
        "mostly_missed_episodes": 1,
        "false_triggers": 3,
        "empty_periods_failed": 1,
        "empty_periods_with_false_trigger": 1,
        "empty_periods_mostly_false_active": 0,
        "longest_miss_seconds": 24,
        "covered_seconds": 240,
        "uncovered_label_seconds": 0,
        "sufficient_statistics": {
            "present/human": [120, 48, 2, 1],
            "not_present/human": [120, 30, 2, 1],
        },
    }


def result(p):
    m = metrics()
    # The evaluator owns these independent fixed-pattern aggregates. They are not
    # synthesized by report formatting, or inferred from pooled time statistics.
    bundle = {
        "human": deepcopy(m),
        "automatic": empty_metrics(),
        "raw": deepcopy(m),
        "recent": deepcopy(m),
    }
    bundle["raw"].update(occupied_recall=0.5, false_active_seconds=0, cost=500)
    bundle["members"] = {
        "r": {
            "metrics": deepcopy(m),
            "exclusive_presence_seconds": 12,
            "missed_seen_seconds": 3,
            "missed_seen_inferred_seconds": 2,
            "missed_seen_automatic_seconds": 1,
        }
    }
    bundle["automatic_evidence"] = {
        "offered_samples": {"present": 0, "not_present": 0},
        "samples": {"present": 0, "not_present": 0},
        "used": False,
        "base_weight": 0,
        "mean_confidence": {"present": None, "not_present": None},
        "share": {"present": 0, "not_present": 0},
        "priority": "human_first",
        "deferred_samples": 0,
    }
    bundle["influence"] = []
    bundle["members"]["r"]["sources"] = {k: deepcopy(bundle[k]) for k in ("human", "automatic")}
    bundle["before"] = deepcopy(bundle)
    return FitResult(
        {"r": {0: 40}},
        deepcopy(m),
        deepcopy(m),
        {
            "score_plan": ScorePlan("exact", ((0, 240),), 4, 2),
            "scoring_mode": "exact",
            "measurements": bundle,
            "zones": {"z": bundle},
            "quiet_cost": 7,
            "selection_cost": 417.25,
        },
    )


def empty_metrics():
    m = metrics()
    for key in m:
        m[key] = 0
    m.update(cost=None, score=None, occupied_recall=None, sufficient_statistics={})
    return m


def report():
    from tuner_under_test.engine.hybrid.reports import device_report

    p = problem()
    return device_report(
        p,
        result(p),
        {
            "selected": "reference",
            "validated": False,
            "reason": "no holdout",
            "cutoff": None,
            "candidates": {},
            "quality_version": 6,
            "provenance": "synthetic",
        },
    )


def test_device_report_retains_all_panel_paths():
    value = report()
    assert_device_contract(value)
    assert value["counts"] == {"present": 20, "not_present": 20}
    assert value["proposals"]["g0_move"]["threshold"] == 40
    assert value["training"]["duration"]["false_positive_score"] == -25
    assert value["training"]["false_negatives"] == 8


def test_room_report_and_member_projection_retain_all_paths():
    from tuner_under_test.engine.hybrid.reports import room_report

    p = problem()
    value = room_report(p, result(p), {"other": "no observed class"}, None)
    assert_room_contract(value["after"])
    assert value["groups"]["other"] == {"skipped": "no observed class"}
    assert value["after"]["devices"]["r"]["missed_seen_seconds"] == 3
    assert value["after"]["devices"]["r"]["exclusive_presence_seconds"] == 12
    assert value["thresholds"] == {"r": {"g0_move": 40}}
    assert value["cost_after"] == 410.25


def test_raw_and_timed_metrics_have_same_evidence():
    value = report()
    assert value["raw_training"]["present_samples"] == value["training"]["present_samples"]
    assert value["raw_training"]["duration"]["presence_recall"] == 0.5
    assert value["training"]["duration"]["presence_recall"] == 0.6
    assert value["raw_training"]["duration"]["false_positive_seconds"] == 0


def test_completely_and_mostly_missed_are_distinct():
    value = report()["training"]
    assert value["missed_presence_episodes"] == 0
    assert value["duration"]["missed_presence_episodes"] == 0
    assert value["mostly_missed_episodes"] == 1
    assert value["false_trigger_bursts"] == 3


def test_recent_training_uses_newest_twenty_percent_per_class():
    from tuner_under_test.engine.hybrid.diagnostics import diagnostics

    p = problem()
    value = diagnostics(p, {"r": {0: 40}}, ScorePlan("exact", ((0, 240),), 4, 2))
    cutoffs = value["radars"]["r"]["recent_cutoffs"]
    assert cutoffs == {"present": 156, "not_present": 216}
    for state, cutoff in cutoffs.items():
        observed = sum(
            min(span.end, p.end) - max(span.start, cutoff)
            for span in p.zones[0].truth
            if span.state == state and span.end > cutoff
        )
        assert observed == 24


def test_diagnostics_are_bounded_and_density_invariant():
    from tuner_under_test.engine.hybrid.diagnostics import diagnostics

    plan = ScorePlan("cells", ((0, 240),), 4, 2)
    a = diagnostics(problem(), {"r": {0: 40}}, plan)
    b = diagnostics(problem(20), {"r": {0: 40}}, plan)
    assert a == b
    assert a["evidence_budget"]["coverage"] == "all_observed_labelled_time"
    assert a["evidence_budget"]["scoring_mode"] == "cells"
    p = problem()
    end = 100000
    spans = (TruthSpan(0, end, "present", "human", 1, "p", ()),)
    p = replace(
        p, end=end, zones=(replace(p.zones[0], truth=spans),), rows={"r": Rows([(0, end, 70)])}
    )
    value = diagnostics(p, {"r": {0: 40}}, plan)["radars"]["r"]
    assert len(value["samples"]["present"]) == 5000
    assert value["sample_counts"]["present"] > 5000
    assert len(value["review"]["sessions"]) <= 24
    assert len(value["review"]["periods"]) <= 24


def test_cost_is_evaluator_cost_not_old_time_formula():
    value = report()
    assert value["training"]["duration"]["error_cost"] == 410.25
    assert value["base_cost"] == 410.25
    assert value["selection_cost"] == 417.25


def test_missing_evaluator_measurement_is_rejected():
    from tuner_under_test.engine.hybrid.reports import device_report

    p = problem()
    r = result(p)
    del r.diagnostics["measurements"]["recent"]
    with pytest.raises(ValueError, match="recent"):
        device_report(p, r, {})


def test_room_before_does_not_reuse_after_source_or_member_outcomes():
    from tuner_under_test.engine.hybrid.reports import room_report

    p = problem()
    r = result(p)
    r = replace(r, before={**r.before, "occupied_recall": 0.2})
    del r.diagnostics["zones"]["z"]["before"]
    with pytest.raises(ValueError, match="before"):
        room_report(p, r, {}, None)


def test_member_source_outcomes_cannot_fall_back_to_room_union():
    from tuner_under_test.engine.hybrid.reports import room_report

    p = problem()
    r = result(p)
    del r.diagnostics["zones"]["z"]["members"]["r"]["sources"]
    with pytest.raises(ValueError, match="sources"):
        room_report(p, r, {}, None)


def test_raw_report_rejects_different_observed_evidence():
    from tuner_under_test.engine.hybrid.reports import device_report

    p = problem()
    r = result(p)
    r.diagnostics["measurements"]["raw"]["occupied_seconds"] = 119
    with pytest.raises(ValueError, match="evidence"):
        device_report(p, r, {})


def test_exclusive_gate_support_excludes_other_raw_gate_detections():
    from tuner_under_test.engine.hybrid.reports import device_report

    p = problem()
    r = result(p)
    # Keep a real bounded diagnostic collection; alter only the synthetic raw
    # samples to add a simultaneously detecting second configured channel.
    from tuner_under_test.engine.hybrid.diagnostics import diagnostics

    d = diagnostics(p, r.thresholds, ScorePlan("exact", (), 4, 2))
    p = replace(p, radars={"r": replace(p.radars["r"], channels=(0, 1), current={0: 50, 1: 50})})
    r.thresholds["r"][1] = 40
    d["radars"]["r"]["histograms"][1] = deepcopy(d["radars"]["r"]["histograms"][0])
    d["radars"]["r"]["samples"]["present"] = list(d["radars"]["r"]["samples"]["present"])
    for row in d["radars"]["r"]["samples"]["present"]:
        row["energies"]["g0_still"] = row["energies"]["g0_move"]
    r.diagnostics["raw_diagnostics"] = d
    value = device_report(p, r, {})
    assert value["proposals"]["g0_move"]["exclusive_presence_samples"] == 0
    assert value["proposals"]["g0_move"]["weakest_exclusive_presence_energy"] is None


def test_diagnostics_recent_window_keeps_scalars_instead_of_all_label_regions():
    from tuner_under_test.engine.hybrid.diagnostics import diagnostics

    value = diagnostics(problem(), {"r": {0: 40}}, ScorePlan("exact", (), 4, 2))["radars"]["r"]
    assert value["recent_cutoffs"] == {"present": 156, "not_present": 216}
    assert "recent_regions" not in value


def test_review_preserves_evaluator_influence_instead_of_empty_placeholder():
    from tuner_under_test.engine.hybrid.reports import device_report

    p = problem()
    r = result(p)
    influence = [
        {
            "start": 120,
            "end": 180,
            "state": "present",
            "error_share_percent": 50,
            "error_seconds": 24,
            "score_without_period": 17,
        }
    ]
    r.diagnostics["measurements"]["influence"] = influence
    value = device_report(p, r, {})
    assert value["review"]["influence"] == influence
    assert value["review"]["periods"][0]["gates"]


def test_automatic_evidence_preserves_independent_support_audit():
    from tuner_under_test.engine.hybrid.reports import device_report

    p = problem()
    r = result(p)
    audit = {
        "offered_samples": {"present": 3, "not_present": 4},
        "samples": {"present": 2, "not_present": 3},
        "used": True,
        "base_weight": 0.7,
        "mean_confidence": {"present": 0.8, "not_present": 0.9},
        "share": {"present": 0.001, "not_present": 0.002},
        "priority": "human_first",
        "deferred_samples": 1,
        "quality": {"independent_support": True},
    }
    r.diagnostics["measurements"]["automatic_evidence"] = audit
    value = device_report(p, r, {})
    assert value["automatic_evidence"] == audit


def test_diagnostic_sample_slots_fit_room_memory_budget():
    import tracemalloc

    from tuner_under_test.engine.hybrid.diagnostics import diagnostics

    class AllRows(Rows):
        def blocks(self, lo, hi):
            for block in super().blocks(lo, hi):
                yield replace(block, columns=(block.columns[0],) * 18)

    p = problem()
    spans = (
        TruthSpan(0, 50000, "present", "human", 1, "p", ()),
        TruthSpan(50000, 100000, "not_present", "human", 1, "e", ()),
    )
    radars = {
        str(i): RadarSpec(str(i), tuple(range(18)), {}, dict.fromkeys(range(18), 40), {})
        for i in range(7)
    }
    p = replace(
        p,
        radars=radars,
        end=100000,
        zones=(ZoneSpec("z", tuple(radars), spans),),
        rows={r: AllRows([(0, 50000, 70), (50000, 100000, 10)]) for r in radars},
    )
    tracemalloc.start()
    try:
        value = diagnostics(
            p, {r: dict.fromkeys(range(18), 40) for r in radars}, ScorePlan("exact", (), 14, 2)
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert all(len(d["samples"]["present"]) == 5000 for d in value["radars"].values())
    assert peak < 24 * 1024 * 1024


def test_review_top_periods_are_largest_errors_not_largest_durations():
    from tuner_under_test.engine.hybrid.diagnostics import diagnostics

    p = problem()
    spans, pieces = [], []
    for i in range(26):
        a = i * 400
        spans.append(TruthSpan(a, a + 300, "present", "human", 1, str(i), ()))
        missed = 1 if i < 24 else 200
        pieces.extend([(a, a + missed, 10), (a + missed, a + 300, 70)])
    p = replace(
        p, end=10400, zones=(ZoneSpec("z", ("r",), tuple(spans)),), rows={"r": Rows(pieces)}
    )
    review = diagnostics(p, {"r": {0: 40}}, ScorePlan("exact", (), 52, 2))["radars"]["r"]["review"]
    assert len(review["periods"]) == 24
    assert review["periods"][0]["errors"] == pytest.approx(200 / 6)


def test_multi_zone_with_one_skipped_has_no_top_level_union_room():
    from tuner_under_test.engine.hybrid.reports import room_report

    p = problem()
    p = replace(p, zones=(*p.zones, ZoneSpec("skip", ("r",), ())))
    r = result(p)
    r = replace(
        r,
        before={"cost": 410.25, "zones": {"z": metrics()}},
        after={"cost": 410.25, "zones": {"z": metrics()}},
    )
    value = room_report(p, r, {"skip": "missing empty class"}, None)
    assert value["after"]["room"] is None
    assert_room_contract(value["groups"]["z"]["after"])


def test_raw_human_measurement_uses_human_evidence_when_automatic_time_exists():
    from tuner_under_test.engine.hybrid.reports import device_report

    p = problem()
    r = result(p)
    r.after["occupied_seconds"] = 150
    r.after["covered_seconds"] = 270
    value = device_report(p, r, {})
    assert value["raw_training"]["duration"]["present_seconds"] == 120
    assert value["training"]["duration"]["present_seconds"] == 120


def test_score_plan_is_required_for_honest_row_budget_audit():
    from tuner_under_test.engine.hybrid.reports import device_report

    p = problem()
    r = result(p)
    r.diagnostics.pop("score_plan", None)
    with pytest.raises(ValueError, match="score_plan"):
        device_report(p, r, {})


def test_zone_assessment_timing_contains_only_its_members():
    from tuner_under_test.engine.hybrid.reports import room_report

    p = problem()
    p = replace(p, radars={**p.radars, "other": replace(p.radars["r"], device_id="other")})
    value = room_report(p, result(p), {}, None)
    assert set(value["after"]["timing"]) == {"r"}


def test_review_period_is_error_substretch_and_reserves_both_classes():
    from tuner_under_test.engine.hybrid.diagnostics import diagnostics

    p = problem()
    spans = [TruthSpan(0, 300, "present", "human", 1, "p", ())]
    pieces = [(0, 10, 10), (10, 300, 70)]
    for i in range(30):
        a = 400 + i * 100
        spans.append(TruthSpan(a, a + 60, "not_present", "human", 1, str(i), ()))
        pieces.append((a, a + 60, 70))
    p = replace(p, end=3400, zones=(ZoneSpec("z", ("r",), tuple(spans)),), rows={"r": Rows(pieces)})
    review = diagnostics(p, {"r": {0: 40}}, ScorePlan("exact", (), 32, 2))["radars"]["r"]["review"]
    quiet = next(row for row in review["periods"] if row["state"] == "present")
    assert (quiet["start"], quiet["end"], quiet["samples"]) == (0, 10, 10 / 6)
    assert review["period_count"] == 31
    assert review["sessions"][0]["errors"] == 10


def test_room_report_keeps_policy_audit_separate_from_anomaly_audit():
    from tuner_under_test.engine.hybrid.reports import room_report

    p = problem()
    r = result(p)
    policy_audit = {"selection": {"policy": "reference", "validated": False}, "quality_version": 6}
    r.diagnostics["automatic_evidence"] = policy_audit
    anomaly = {"status": "discarded", "discarded_count": 1}
    value = room_report(p, r, {}, anomaly)
    assert value["automatic_evidence"] == policy_audit
    assert value["anomaly"] == anomaly
    assert value["validation_scope"] == "estimated_without_independent_validation"


@pytest.mark.parametrize("mode", ["exact", "cells"])
@pytest.mark.parametrize("human_states", [(), ("present",), ("present", "not_present")])
def test_room_assessment_preserves_evidence_basis_separately_from_fidelity(mode, human_states):
    from tuner_under_test.engine.hybrid import reports

    found = metrics()
    human, automatic = empty_metrics(), metrics()
    for state in human_states:
        key = "occupied_seconds" if state == "present" else "empty_seconds"
        human[key] = found[key]
        automatic[key] = 0
    assessment = reports._assessment(found, {"human": human, "automatic": automatic}, mode)
    assert assessment["basis"] == (
        "human" if set(human_states) == {"present", "not_present"} else "estimated"
    )
    assert assessment["scoring_mode"] == mode
