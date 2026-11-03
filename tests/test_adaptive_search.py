"""Native automatic support never censors human observations or manufactures votes."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
import test_tuner as harness
from tuner_under_test.calibration.quality import learn
from tuner_under_test.calibration.quality_evidence import collect
from tuner_under_test.engine.hybrid import adapters, engine, facade
from tuner_under_test.engine.hybrid.exact import Evaluator
from tuner_under_test.engine.hybrid.fixed import source
from tuner_under_test.engine.hybrid.replay import TABLES, support_tables


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
    context = collect(view, "d", ["g0_move"], start=0, end=view._history_until)
    # Independent quiet reference: automatic energy must exceed20 plus the3-point margin.
    context = replace(
        context,
        gate_empty={"g0_move": [1000 if i == 20 else 0 for i in range(101)]},
        gate_empty_histograms={},
    )
    return adapters.device_problem(
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
        reference=reference or weight == 0,
    )


def scored(problem, threshold, origin=None):
    zones = None if origin is None else (source(problem.zones[0], origin),)
    with Evaluator(problem, ((problem.start, problem.end),)) as evaluator:
        return evaluator.scores([{"d": {0: threshold}}], zones=zones)[0]


def test_gate_support_changes_only_automatic_present_hits():
    problem = scene()
    model = problem.policy.quality["models"]["d"]
    tables = support_tables(problem.radars["d"], model)[0]
    for threshold in range(101):
        assert TABLES[threshold][:101] == bytes(int(value > threshold) for value in range(101))
        assert tables[threshold][:101] == bytes(
            int(value > threshold and model.supports("g0_move", value)) for value in range(101)
        )
    assert not tables[0][20] and tables[0][21]
    human = scored(problem, 12, "human")
    assert human["missed_seconds"] == human["false_active_seconds"] == 0


def test_blend_uses_independent_episode_support():
    item = scene()
    human, automatic = scored(item, 12, "human"), scored(item, 12, "automatic")
    expected = 0.5 * 0.25 * 0.6 / 1.6
    for state in ("present", "not_present"):
        assert facade._share(item.policy, state, human, automatic) == pytest.approx(expected)


def test_reference_removes_automatic_preferences_and_ties_for_supported_classes():
    reference, plain = scene(reference=True, weight=1), scene(automatic=False, weight=0)
    assert engine.fit(reference).thresholds == engine.fit(plain).thresholds
    for threshold in (0, 12, 50):
        assert scored(reference, threshold)["cost"] == scored(plain, threshold)["cost"]


def test_reference_keeps_only_automatic_source_for_missing_human_class():
    item = scene(positive=False, reference=True)
    found = engine.fit(item).thresholds["d"][0]
    assert 10 <= found < 30
    assert scored(item, found, "automatic")["occupied_seconds"] == 18
    assert scored(item, found, "human")["occupied_seconds"] == 0


def test_human_supported_low_gate_remains_reachable():
    item = scene()
    found = engine.fit(item).thresholds["d"][0]
    assert found < 15
    assert scored(item, found, "human")["occupied_recall"] == 1


def test_zero_ceiling_has_no_automatic_tie_influence_on_human_classes():
    reference, plain = scene(weight=0), scene(automatic=False, weight=0)
    assert engine.fit(reference).thresholds == engine.fit(plain).thresholds
    for threshold in (0, 12, 50):
        assert scored(reference, threshold)["cost"] == scored(plain, threshold)["cost"]
