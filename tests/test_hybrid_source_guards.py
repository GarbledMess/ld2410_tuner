"""Selection guards apply to active evidence sources while raw diagnostics stay complete."""

import pytest
import test_tuner  # noqa: F401
from test_hybrid_evidence import truth
from test_hybrid_selection import fit_problem
from tuner_under_test.engine.hybrid import engine, exact, selection
from tuner_under_test.engine.hybrid.types import EvidencePolicy


@pytest.mark.parametrize(
    "policy",
    [
        EvidencePolicy(0, False, {}),
        EvidencePolicy(1, True, {}),
        EvidencePolicy(
            1,
            False,
            {
                "human_episodes": {"present": 1, "not_present": 1},
                "automatic_episode_mass": {"not_present": 0},
            },
        ),
    ],
)
def test_suppressed_automatic_empty_does_not_veto_human_presence(policy):
    rows = [
        (0, 600, [28] + [255] * 17),
        (600, 1200, [25] + [255] * 17),
        (1200, 1800, [5] + [255] * 17),
    ]
    spans = (
        truth(0, 600, "not_present", source="automatic", episode="auto-empty"),
        truth(600, 1200, "present", episode="human-present"),
        truth(1200, 1800, "not_present", episode="human-empty"),
    )
    result = engine.fit(fit_problem(rows, spans, current=50, policy=policy), audit=False)
    assert result.after["cost"] == 0
    assert result.thresholds["a"][0] < 25
    assert (
        result.after["empty_periods_mostly_false_active"] == 1
    )  # Raw diagnostic remains truthful.
    assert result.after["guard_counts"]["empty_periods_mostly_false_active"] == 0


@pytest.mark.parametrize(
    ("human_empty", "policy"),
    [(True, EvidencePolicy(1, False, {})), (False, EvidencePolicy(0, True, {}))],
)
def test_participating_automatic_empty_still_guards_false_activation(human_empty, policy):
    rows = [
        (0, 600, [28] + [255] * 17),
        (600, 1200, [25] + [255] * 17),
        (1200, 1800, [5] + [255] * 17),
    ]
    spans = [
        truth(0, 600, "not_present", source="automatic", episode="auto-empty"),
        truth(600, 1200, "present", episode="human-present"),
    ]
    if human_empty:
        spans.append(truth(1200, 1800, "not_present", episode="human-empty"))
    problem = fit_problem(rows, tuple(spans), current=50, policy=policy)
    patterns = [{"a": {0: 50}}, {"a": {0: 10}}]
    with exact.Evaluator(problem, ((0, 1800),)) as evaluator:
        scores = evaluator.scores(patterns)
    assert scores[1]["guard_counts"]["empty_periods_mostly_false_active"] == 1
    assert selection.choose(problem, patterns, scores, None, {})[0] == 0
