"""Hybrid candidate and fixed-pattern work is bounded and independent of shard layout."""

import random
from dataclasses import replace

import pytest
import test_tuner  # noqa: F401
from test_hybrid_exact import Rows
from test_hybrid_search import problem
from tuner_under_test.calibration.duration import _confidence_time
from tuner_under_test.calibration.intervals import WeightedTime
from tuner_under_test.engine.hybrid import engine
from tuner_under_test.engine.hybrid.exact import Evaluator
from tuner_under_test.engine.hybrid.search import collect_training, search
from tuner_under_test.engine.hybrid.training import TrainingStore
from tuner_under_test.engine.hybrid.types import EvidencePolicy


@pytest.mark.parametrize("weight", [0, 0.5, 1])
@pytest.mark.parametrize("shard_cells", [1, 7, 4096])
def test_deduplicated_gate_sweeps_match_shared_fixed_evaluation(tmp_path, weight, shard_cells):
    rng = random.Random(9050)
    vectors = [(rng.choice((60, 80)), rng.choice((0, 70))) for _ in range(30)]
    vectors += [(rng.choice((0, 20)), rng.choice((0, 30))) for _ in range(30)]
    p = problem({"r": vectors}, current=90)
    p = replace(p, rows={"r": Rows(p.days.rows["r"])}, policy=EvidencePolicy(weight, False, {}))
    with TrainingStore(tmp_path) as store:
        collect_training(p, store, shard_cells=shard_cells)
        candidates = search(p, store)
    with TrainingStore(tmp_path) as store:
        collect_training(p, store)
        assert search(p, store) == candidates
    assert candidates[0] == {"r": {0: 90, 1: 90}}
    assert len({tuple(sorted(pattern["r"].items())) for pattern in candidates}) == len(candidates)
    with Evaluator(p, ((0, 360),)) as evaluator:
        scores = evaluator.scores(candidates)
        assert evaluator.scores(candidates) == scores
    for pattern, score in zip(candidates, scores, strict=True):
        assert all(
            value in p.radars["r"].domains[channel] for channel, value in pattern["r"].items()
        )
        assert score["cost"] == pytest.approx(100 * (score["time_term"] + score["episode_term"]))
    fitted = engine.fit(p)
    assert fitted.after["cost"] <= fitted.before["cost"]
    assert fitted.after["mostly_missed_episodes"] <= fitted.before["mostly_missed_episodes"]


def test_confidence_endpoints_are_integrated_once_across_candidate_intervals():
    confidence = _confidence_time(rows(500, 0, "present", 0.83))
    intervals = [(index * 6 + 0.25, index * 6 + 4.75) for index in range(400)]
    first = [confidence.between(start, end) for start, end in intervals]
    misses = confidence.at.cache_info().misses
    assert [confidence.between(start, end) for start, end in intervals] == first
    assert confidence.at.cache_info().misses == misses


def test_remembered_endpoint_integrals_are_byte_identical_to_plain_prefix_integrals():
    rng = random.Random(8160)
    observed = [(index * 6 + rng.random(), {}, "present", rng.random()) for index in range(500)]
    confidence = _confidence_time(observed)
    plain = WeightedTime(confidence.times, confidence.weights)
    for _ in range(300):
        first, last = sorted((rng.uniform(-5, 3005), rng.uniform(-5, 3005)))
        assert confidence.between(first, last) == plain.between(first, last)


def rows(count, start, state, confidence=None):
    return [
        (
            start + index * 6,
            {"g0_move": index % 101},
            state,
            confidence if confidence is not None else 1.0,
        )
        for index in range(count)
    ]
