"""Pure chronological candidate selection regressions."""

import pytest
import test_tuner  # noqa: F401  # Registers tuner_under_test without HA.


def select(*args, **kwargs):
    from tuner_under_test.calibration.selection import select as choose

    return choose(*args, **kwargs)


def report(missed=2.0, episodes=0, false=10.0, **extra):
    return dict(missed_seconds=missed, missed_episodes=episodes, false_seconds=false, **extra)


def test_better_total_does_not_hide_new_empty_episode_harm():
    picked = select(report(), report(1.0, false=9.0), ((10.0, 0.0), (0.0, 9.0)), measurable=True)
    assert picked.policy == "reference"
    assert picked.validated


def test_missing_validation_is_not_zero_accuracy():
    picked = select(None, None, (), measurable=False)
    assert picked.policy == "corrected_positive"
    assert picked.validated is False
    assert picked.reason


@pytest.mark.parametrize(
    "positive,pairs,policy",
    [
        (report(1.0, false=9.0), ((10.0, 9.0),), "corrected_positive"),
        (report(2.0, false=9.0), ((10.0, 9.0),), "corrected_positive"),
        (report(3.0, false=0.0), ((10.0, 0.0),), "reference"),
        (report(1.0, episodes=1, false=0.0), ((10.0, 0.0),), "reference"),
        (report(1.0, false=11.0), ((10.0, 11.0),), "reference"),
        (report(), ((10.0, 10.0),), "reference"),
        (report(2.0 - 5e-10, false=10.0), ((10.0, 10.0),), "reference"),
        (report(1.0, false=10.0 + 5e-10), ((10.0, 10.0 + 5e-10),), "corrected_positive"),
    ],
)
def test_duration_dominance_and_float_ties(positive, pairs, policy):
    picked = select(report(), positive, pairs, measurable=True)
    assert picked.policy == policy
    assert picked.validated


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        report(float("nan")),
        report(-1.0),
        report(false=float("inf")),
        report(episodes=0.5),
        report(missed=True),
        report(present_seconds=0.0),
        report(empty_seconds=0.0),
        report(unscored_empty_samples=1),
    ],
)
def test_invalid_or_unmeasurable_reports_are_provisional(bad):
    picked = select(report(), bad, ((10.0, 0.0),), measurable=True)
    assert picked.policy == "corrected_positive"
    assert not picked.validated
    assert picked.reason


@pytest.mark.parametrize(
    "pairs", [None, (1.0,), (), ((0.0,),), ((-1.0, 0.0),), ((0.0, float("nan")),)]
)
def test_missing_or_invalid_empty_episode_reports_are_provisional(pairs):
    picked = select(report(), report(1.0), pairs, measurable=True)
    assert picked.policy == "corrected_positive"
    assert not picked.validated


def test_actual_duration_report_field_names_are_accepted():
    ref = {
        "missed_seconds": 2.0,
        "missed_presence_episodes": 0,
        "false_positive_seconds": 10.0,
        "present_seconds": 20.0,
        "empty_seconds": 20.0,
        "unscored_presence_samples": 0,
    }
    pos = dict(ref, missed_seconds=1.0)
    picked = select(ref, pos, ((10.0, 10.0),), measurable=True)
    assert picked.policy == "corrected_positive"
    assert picked.validated


def hybrid(missed=2.0, false=10.0, cost=20.0, **counts):
    """A held-out hybrid score: durations plus the common cost and episode counts."""
    found = {
        "mostly_missed_episodes": 0,
        "empty_periods_mostly_false_active": 0,
        "empty_periods_failed": 0,
    }
    found.update(counts)
    return report(missed, false=false, cost=cost, **found)


def test_hybrid_scores_choose_on_the_common_cost():
    picked = select(hybrid(), hybrid(cost=19.0), ((10.0, 10.0),), measurable=True)
    assert (picked.policy, picked.reason) == ("corrected_positive", "held_cost_improvement")
    # Lower missed time alone is not enough when the common cost does not fall.
    tie = select(hybrid(), hybrid(1.0, cost=20.0), ((10.0, 10.0),), measurable=True)
    assert (tie.policy, tie.reason) == ("reference", "held_cost_tie")
    almost = select(hybrid(), hybrid(cost=20.0 - 5e-10), ((10.0, 10.0),), measurable=True)
    assert almost.policy == "reference"


@pytest.mark.parametrize(
    "positive,reason",
    [
        (hybrid(1.0, cost=1.0, mostly_missed_episodes=1), "held_presence_regression"),
        (hybrid(1.0, cost=1.0, empty_periods_failed=1), "held_empty_episode_regression"),
        (
            hybrid(1.0, cost=1.0, empty_periods_mostly_false_active=1),
            "held_empty_episode_regression",
        ),
        (hybrid(3.0, cost=1.0), "held_presence_regression"),
        (hybrid(1.0, false=11.0, cost=1.0), "held_empty_duration_regression"),
    ],
)
def test_a_lower_hybrid_cost_cannot_hide_episode_or_duration_harm(positive, reason):
    picked = select(hybrid(), positive, ((10.0, 10.0),), measurable=True)
    assert (picked.policy, picked.reason) == ("reference", reason)
    assert picked.validated


def test_hybrid_scores_keep_the_per_empty_episode_guard():
    picked = select(hybrid(), hybrid(cost=1.0), ((10.0, 0.0), (0.0, 10.0)), measurable=True)
    assert (picked.policy, picked.reason) == ("reference", "held_empty_episode_regression")


@pytest.mark.parametrize(
    "bad",
    [
        hybrid(cost=None),
        hybrid(cost=float("nan")),
        hybrid(cost=-1.0),
        hybrid(mostly_missed_episodes=0.5),
        {key: value for key, value in hybrid().items() if key != "empty_periods_failed"},
        report(),  # A legacy duration report cannot be ranked against a hybrid one.
    ],
)
def test_invalid_hybrid_scores_are_provisional(bad):
    picked = select(hybrid(), bad, ((10.0, 10.0),), measurable=True)
    assert picked.policy == "corrected_positive"
    assert not picked.validated
