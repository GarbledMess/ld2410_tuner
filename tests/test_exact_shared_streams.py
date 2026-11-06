"""Patterns detecting alike are scored once, and every score stays what it was."""

from dataclasses import replace
from unittest.mock import patch

import pytest
import test_tuner  # noqa: F401
from real_rows import THRESHOLD, RealRows, real_problem
from tuner_under_test.engine.hybrid import exact, scoring
from tuner_under_test.engine.hybrid.exact import Evaluator
from tuner_under_test.engine.hybrid.types import ZoneSpec

ROWS = 30_000
# The fixture reads at most 35 quiet and at least 41 busy: thresholds 35 to 40 detect alike.
ALIKE = (35, 36, THRESHOLD)
OTHER = (20, 60)


@pytest.fixture(scope="module")
def problem():
    return real_problem(ROWS, channels=4)[0]


def _pattern(problem, threshold):
    return {"r": dict.fromkeys(problem.radars["r"].channels, threshold)}


def _regions(problem):
    return exact.regions(span for zone in problem.zones for span in zone.truth)


def _scores(problem, thresholds, **options):
    with Evaluator(problem, _regions(problem)) as evaluator:
        return evaluator.scores([_pattern(problem, t) for t in thresholds], **options)


def _together_and_apart(problem, thresholds, **options):
    together = _scores(problem, thresholds, **options)
    with patch.object(Evaluator, "alike", lambda _self, _rid, key: key):
        apart = _scores(problem, thresholds, **options)
    return together, apart


def _built(problem, thresholds, **options):
    made = []
    plain = scoring.SliceScorer.__init__

    def init(self, *args, **kwargs):
        made.append(1)
        plain(self, *args, **kwargs)

    with patch.object(scoring.SliceScorer, "__init__", init):
        found = _scores(problem, thresholds, **options)
    return found, len(made)


def test_patterns_detecting_alike_build_one_scorer(problem):
    found, made = _built(problem, ALIKE)
    assert made == 1
    assert found[0]["zones"] == found[1]["zones"] == found[2]["zones"]
    assert found[0]["cost"] is not None


def test_patterns_detecting_differently_each_get_a_scorer(problem):
    found, made = _built(problem, (*ALIKE, *OTHER))
    assert made == 3
    assert len({repr(score["zones"]) for score in found}) == 3


def test_a_score_shared_is_the_score_given_alone(problem):
    together, apart = _together_and_apart(problem, (*ALIKE, *OTHER), quiet=True)
    assert repr(together) == repr(apart)


def test_a_separate_replay_of_the_same_detection_shares_a_scorer(problem):
    with Evaluator(problem, _regions(problem)) as evaluator:
        evaluator.replay([_pattern(problem, ALIKE[0])])
        evaluator.replay([_pattern(problem, ALIKE[1])])
        assert len(evaluator.runs) == 2
        key = evaluator.key(_pattern(problem, ALIKE[1]), "r")
        assert evaluator.alike("r", key) == evaluator.key(_pattern(problem, ALIKE[0]), "r")


def test_timing_keeps_streams_apart_only_where_they_differ(problem):
    timed = replace(
        problem,
        radars={"r": replace(problem.radars["r"], timing={"hold": 5, "on": 1, "off": 2})},
    )
    found, made = _built(timed, (*ALIKE, OTHER[0]))
    assert made == 2
    assert found[0]["zones"] == found[1]["zones"]
    together, apart = _together_and_apart(timed, (*ALIKE, OTHER[0]))
    assert repr(together) == repr(apart)


def test_one_chunk_detecting_differently_keeps_two_streams_apart(problem):
    """A row in only one chunk reads between the two thresholds."""
    stored = problem.rows["r"].stored
    edited = bytearray(stored[-1].columns[0])
    edited[-1] = 36
    last = replace(stored[-1], columns=(bytes(edited), *stored[-1].columns[1:]))
    changed = replace(problem, rows={"r": RealRows([*stored[:-1], last])})
    together, apart = _together_and_apart(changed, (35, 36))
    assert repr(together) == repr(apart)
    _, made = _built(changed, (35, 36))
    assert made == 2


def test_zones_of_different_truth_are_not_shared(problem):
    zone = problem.zones[0]
    human = ZoneSpec("human", zone.members, tuple(s for s in zone.truth if s.source == "human"))
    both = replace(problem, zones=(zone, human))
    _, made = _built(both, ALIKE)
    assert made == 2
    together, apart = _together_and_apart(both, ALIKE)
    assert repr(together) == repr(apart)


def test_scores_stay_the_same_when_the_memory_budget_is_spent(problem, monkeypatch):
    """Each pattern's scorer keeps the storage it had, so its rounding stays its own."""
    labels = scoring.EpisodeLabels(exact._labels(problem.zones[0].truth))
    monkeypatch.setattr(scoring, "EPISODE_MEMORY_BYTES", 32 * len(labels.names) * 2)
    together, apart = _together_and_apart(problem, (*ALIKE, *OTHER))
    assert repr(together) == repr(apart)
    _, made = _built(problem, (*ALIKE, *OTHER))
    # The first two patterns fit the budget and share; none of the others may share with them.
    assert made == 4


def test_nothing_is_replayed_twice_for_patterns_detecting_alike(problem):
    with Evaluator(problem, _regions(problem)) as evaluator:
        evaluator.scores([_pattern(problem, ALIKE[0])])
        runs = len(evaluator.runs)
        evaluator.scores([_pattern(problem, ALIKE[0])])
        assert len(evaluator.runs) == runs
