"""Native automatic support never censors human observations or manufactures votes."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
import test_tuner as harness
from tuner_under_test.calibration import event_fitting
from tuner_under_test.calibration.quality import learn
from tuner_under_test.calibration.quality_evidence import collect
from tuner_under_test.rooms import fitting


def scene(*, positive=True, automatic=True, reference=False, weight=0.5):
    human = [(i * 6, {"g0_move": 15}, "present") for i in range(60)] if positive else []
    human += [(1000 + i * 6, {"g0_move": 10}, "not_present") for i in range(60)]
    guessed = (
        [(2000 + i * 6, {"g0_move": value}, "present", 0.8) for i, value in enumerate((5, 15, 30))]
        if automatic
        else []
    )
    if automatic:
        guessed += [(3000 + i * 6, {"g0_move": 12}, "not_present", 0.8) for i in range(3)]
    view = SimpleNamespace(data={"devices": {"d": {}}, "room_snapshot": None})
    harness.record_samples(view, "d", human, guessed)
    context = collect(view, "d", ["g0_move"])
    # Independent quiet reference: automatic energy must exceed20 plus the3-point margin.
    context = replace(
        context,
        gate_empty={"g0_move": [1000 if i == 20 else 0 for i in range(101)]},
        gate_empty_histograms={},
    )
    objective = event_fitting._objective(
        view,
        "d",
        ["g0_move"],
        {"g0_move": 50},
        {},
        weight,
        {},
        0,
        view._history_until,
        quality_context=learn(context),
    )
    event_fitting._reference(objective, reference, weight)
    return objective


def test_gate_support_changes_only_automatic_present_hits():
    item = scene()
    radar = item.radars["d"]
    rows = item.physical_inputs["rows"]
    for threshold in range(101):
        assert radar.tables["g0_move"][threshold] == sum(
            1 << i for i, (_, values, *_) in enumerate(rows) if values["g0_move"] > threshold
        )
        assert radar.automatic_tables["g0_move"][threshold] == sum(
            1 << i
            for i, (_, values, *_) in enumerate(rows)
            if values["g0_move"] > threshold and values["g0_move"] > 23
        )
    measured = event_fitting._measure(item, {"g0_move": 12})
    assert measured["false_negatives"] == 0  # Human15 remains usable despite automatic veto.
    assert measured["false_positives"] == 0


def test_blend_uses_independent_episode_support():
    item = scene()
    # One human episode and one automatic episode per class;0.8 raw *0.75 prior.
    assert item.shares == pytest.approx((0.5 * 0.25 * 0.6 / 1.6,) * 2)


def test_reference_removes_automatic_preferences_and_ties_for_supported_classes():
    reference, plain = scene(reference=True, weight=1), scene(automatic=False, weight=0)
    assert fitting.search(reference) == fitting.search(plain)
    assert reference.preferred == plain.preferred
    assert reference.shares == (0, 0)


def test_reference_keeps_only_automatic_source_for_missing_human_class():
    item = scene(positive=False, reference=True)
    assert item.sources == ("automatic", "human")
    result = fitting.search(item)["d"]
    assert 10 <= result["g0_move"] < 30
    assert event_fitting._measure(item, result, "automatic")["present_samples"] == 3


def test_human_supported_low_gate_remains_reachable():
    item = scene()
    found = fitting.search(item)["d"]
    assert found["g0_move"] < 15


def test_zero_ceiling_has_no_automatic_tie_influence_on_human_classes():
    reference, plain = scene(weight=0), scene(automatic=False, weight=0)
    assert reference.shares == (0, 0)
    assert fitting.search(reference) == fitting.search(plain)
