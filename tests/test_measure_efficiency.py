"""Streaming hybrid source and episode measurements match scalar interval oracles."""

import random

import pytest
import test_tuner  # noqa: F401
from test_hybrid_evidence import truth
from test_hybrid_scoring import evaluate, oracle
from tuner_under_test.engine.hybrid.types import EvidencePolicy
from tuner_under_test.rooms import intervals


def scene():
    rng = random.Random(4)
    spans = [
        truth(
            i * 10,
            i * 10 + 7,
            "present" if i % 3 else "not_present",
            "automatic" if i % 5 else "human",
            rng.uniform(0.55, 1),
            str(i),
        )
        for i in range(200)
    ]
    coverage = {"r": [(i * 25, i * 25 + 20) for i in range(80)]}
    active = {"r": [(i * 13 + 0.25, i * 13 + 4.75) for i in range(150)]}
    return spans, coverage, active


def test_source_totals_equal_the_per_region_definition_without_a_pass_per_region(tmp_path):
    spans, coverage, active = scene()
    result = evaluate(
        tmp_path,
        spans,
        active,
        cuts=(0, 500, 1000, 1500, 2000),
        coverage=coverage,
        policy=EvidencePolicy(0.5, False, {}),
    )
    for state in ("present", "not_present"):
        for source in ("human", "automatic"):
            expected = sum(
                (b - a) * span.confidence
                for span in spans
                if span.state == state and span.source == source
                for a, b in intervals.intersect(coverage["r"], [(span.start, span.end)])
            )
            assert result["sufficient_statistics"][f"{state}/{source}"][0] == pytest.approx(
                expected
            )


@pytest.mark.parametrize("weight", [0, 0.5, 1])
def test_source_rates_integrate_each_span_list_once_with_identical_results(tmp_path, weight):
    spans, coverage, active = scene()
    expected = oracle(spans, active, coverage, (0, 2000), weight)
    actual = evaluate(
        tmp_path,
        spans,
        active,
        cuts=tuple(range(0, 2001, 100)),
        coverage=coverage,
        policy=EvidencePolicy(weight, False, {}),
    )
    for key in ("cost", "time_term", "episode_term", "longest_miss_seconds"):
        assert actual[key] == pytest.approx(expected[key])
    for key, value in expected["sufficient_statistics"].items():
        assert actual["sufficient_statistics"][key] == pytest.approx(value)


def test_longest_inactive_and_episode_pieces_equal_the_per_interval_definition(tmp_path):
    rng = random.Random(7)
    spans = [truth(i * 20, i * 20 + 15, episode=str(i)) for i in range(40)]
    spans.append(truth(800, 820, "not_present", episode="empty"))
    active = {"r": intervals.union((i, i + rng.uniform(0.1, 4)) for i in range(0, 820, 7))}
    expected = oracle(spans, active, {"r": [(0, 820)]}, (0, 820))
    actual = evaluate(tmp_path, spans, active, cuts=tuple(range(0, 821, 10)))
    for key in ("longest_miss_seconds", "occupied_episodes", "mostly_missed_episodes", "cost"):
        assert actual[key] == pytest.approx(expected[key])

    assert actual["completely_missed_episodes"] == sum(
        not intervals.intersect(active["r"], [(s.start, s.end)])
        for s in spans
        if s.state == "present"
    )
