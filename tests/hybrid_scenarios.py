"""Synthetic report builders and reusable compatibility assertions.

These helpers describe public report shapes, independently of any fitting engine.
They intentionally accept additive fields and older saved model identities.
"""

from copy import deepcopy
from math import isfinite

CLASSES = ("present", "not_present")
MEASUREMENT = """duration present_samples not_present_samples sensitivity false_positive_rate
false_negatives false_positives presence_episodes missed_presence_episodes
longest_missed_run_samples false_trigger_bursts false_trigger_bursts_per_hour
observed_absent_seconds""".split()
DURATION = """present_seconds empty_seconds missed_seconds missed_seconds_upper
false_positive_seconds false_positive_percent false_positive_score presence_recall
presence_recall_lower presence_episodes missed_presence_episodes false_trigger_events
unscored_presence_samples unscored_empty_samples basis missed_time_cost longest_missed_seconds
longest_missed_seconds_upper error_cost""".split()
DEVICE = """counts keys warnings count_basis evidence_basis training estimated_training
recent_training validation targets validation_scope timing
adaptive_policy automatic_evidence review outlier_filter feasibility proposals""".split()
PROPOSAL = """threshold status evidence_basis auto_used message role separation
exclusive_presence_samples weakest_exclusive_presence_energy present_samples not_present_samples
sensitivity false_positive_rate false_negatives false_positives specificity false_negative_rate
noise_ceiling noise_floor_p99 noise_source safety_margin""".split()
ASSESSMENT = """outcomes sources score presence_recall false_positive_percent target_met
basis missed_time_cost""".split()
MEMBER = """exclusive_presence_seconds missed_seen_seconds missed_seen_inferred_seconds
missed_seen_automatic_seconds false_active_seconds occupied_seconds empty_seconds""".split()


def _fields(value, names, path):
    assert isinstance(value, dict), path
    for name in names:
        assert name in value, f"{path}.{name}"


def _finite(value):
    assert isinstance(value, (int, float)) and isfinite(value)


def _measurement(value, path):
    _fields(value, MEASUREMENT, path)
    duration = value["duration"]
    _fields(duration, DURATION, path + ".duration")
    for name in DURATION:
        if name != "basis" and duration[name] is not None:
            _finite(duration[name])
    assert duration["present_seconds"] >= duration["missed_seconds"] >= 0
    assert duration["empty_seconds"] >= duration["false_positive_seconds"] >= 0
    assert value["present_samples"] == duration["present_seconds"] / 6
    assert value["not_present_samples"] == duration["empty_seconds"] / 6
    if duration["empty_seconds"]:
        percent = 100 * duration["false_positive_seconds"] / duration["empty_seconds"]
        assert abs(duration["false_positive_percent"] - percent) < 1e-8
        assert abs(duration["false_positive_score"] + percent) < 1e-8
    assert duration["longest_missed_seconds_upper"] >= duration["longest_missed_seconds"] >= 0
    assert duration["missed_presence_episodes"] <= duration["presence_episodes"]


def assert_device_contract(report: dict) -> None:
    """Check panel-facing paths and native seconds/sample/percentage units."""
    _fields(report, DEVICE, "device")
    _fields(report["counts"], CLASSES, "counts")
    for name in ("training", "estimated_training", "recent_training"):
        _measurement(report[name], name)
    for name in ("validation", "current_validation", "raw_training"):
        if report.get(name) is not None:
            _measurement(report[name], name)
    for gate, proposal in report["proposals"].items():
        _fields(proposal, PROPOSAL, f"proposals.{gate}")
        _fields(
            proposal["separation"],
            "preferred_threshold noise_ceiling presence_reference separated human_supported".split(),
            "separation",
        )
    _fields(
        report["automatic_evidence"],
        "offered_samples samples used base_weight mean_confidence share priority deferred_samples".split(),
        "automatic_evidence",
    )
    _fields(
        report["adaptive_policy"],
        "selected validated reason cutoff candidates quality_version provenance".split(),
        "adaptive_policy",
    )
    _fields(
        report["review"],
        "sessions periods period_count influence excluded_automatically".split(),
        "review",
    )
    _fields(report["outlier_filter"], ("method", "excluded"), "outlier_filter")
    _fields(report["feasibility"], ("status", "windows", "reason"), "feasibility")


def _assessment(value):
    _fields(value, ASSESSMENT, "assessment")
    for source in ("human", "automatic"):
        _fields(value["outcomes"], (source,), "outcomes")
        for state in CLASSES:
            outcome = value["outcomes"][source][state]
            _fields(
                outcome,
                "seconds active_seconds possible_seconds weighted_seconds weighted_active_seconds samples hits".split(),
                "outcome",
            )
            assert outcome["seconds"] >= outcome["active_seconds"] >= 0
            assert abs(outcome["samples"] - outcome["seconds"] / 6) < 1e-8


def assert_room_contract(report: dict) -> None:
    """Check assessment, member outcomes and Seen-by attribution fields."""
    _fields(
        report,
        "scorer_version start end room devices excluded shared_recording_seconds timing sample_basis label_basis".split(),
        "room_report",
    )
    _fields(report["excluded"], ("no_shared_recording_seconds", "unlabelled_seconds"), "excluded")
    _assessment(report["room"])
    for member in report["devices"].values():
        _assessment(member)
        _fields(member, MEMBER, "member")


def synthetic_device_report():
    """A complete synthetic native report with no captured history dependency."""
    duration = dict.fromkeys(DURATION, 0)
    duration.update(
        present_seconds=60,
        empty_seconds=60,
        presence_recall=1,
        presence_recall_lower=1,
        basis="physical_interval_time",
        missed_time_cost=5,
    )
    measurement = dict.fromkeys(MEASUREMENT, 0)
    measurement.update(
        duration=duration,
        present_samples=10,
        not_present_samples=10,
        sensitivity=1,
        observed_absent_seconds=60,
    )
    report = dict.fromkeys(DEVICE)
    for name in (
        "training",
        "estimated_training",
        "recent_training",
        "raw_training",
        "validation",
        "current_validation",
    ):
        report[name] = deepcopy(measurement)
    report.update(
        counts=dict.fromkeys(CLASSES, 10),
        keys=["g0_move"],
        warnings=[],
        count_basis="observed_seconds / 6",
        evidence_basis="human",
        targets={},
        validation_scope="synthetic",
        timing={},
        proposals={},
    )
    report["automatic_evidence"] = {
        "offered_samples": dict.fromkeys(CLASSES, 0),
        "samples": dict.fromkeys(CLASSES, 0),
        "used": False,
        "base_weight": 0,
        "mean_confidence": dict.fromkeys(CLASSES),
        "share": dict.fromkeys(CLASSES, 0),
        "priority": "human_first",
        "deferred_samples": 0,
    }
    report["adaptive_policy"] = {
        "selected": "reference",
        "validated": False,
        "reason": "synthetic",
        "cutoff": None,
        "candidates": {},
        "quality_version": 5,
        "provenance": "synthetic",
    }
    report["review"] = {
        "sessions": [],
        "periods": [],
        "period_count": 0,
        "influence": [],
        "excluded_automatically": False,
    }
    report["outlier_filter"] = {"method": "none", "excluded": 0}
    report["feasibility"] = {"status": "not_assessed", "windows": [], "reason": "synthetic"}
    proposal = dict.fromkeys(PROPOSAL, 0)
    proposal.update(
        threshold=40,
        status="ok",
        evidence_basis="human",
        auto_used=False,
        message="Synthetic separating gate",
        role="detection",
        noise_source="human",
        separation={
            "preferred_threshold": 40,
            "noise_ceiling": 10,
            "presence_reference": 70,
            "separated": True,
            "human_supported": True,
        },
    )
    report["proposals"] = {"g0_move": proposal}
    return report
