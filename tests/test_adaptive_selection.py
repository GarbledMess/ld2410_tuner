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
