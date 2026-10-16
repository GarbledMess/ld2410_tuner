"""Conservative selection on later explicit episode durations."""

import math
from collections.abc import Mapping
from dataclasses import dataclass

TOLERANCE = 1e-9


@dataclass(frozen=True)
class Selection:
    policy: str
    validated: bool
    reason: str


def _number(value):
    """Reject missing, negative and nonfinite duration/count observations."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def _metrics(report):
    if not isinstance(report, Mapping):
        return None
    values = (
        report.get("missed_seconds"),
        report.get("missed_episodes", report.get("missed_presence_episodes")),
        report.get("false_seconds", report.get("false_positive_seconds")),
    )
    if not all(_number(value) for value in values):
        return None
    if not float(values[1]).is_integer():
        return None
    return values


def _limitation(report):
    for key in ("present_seconds", "empty_seconds"):
        if key in report and (not _number(report[key]) or report[key] == 0):
            return "zero_or_invalid_validation_time"
    for key in ("unscored_presence_samples", "unscored_empty_samples"):
        if key in report and (not _number(report[key]) or report[key] != 0):
            return "unscored_validation_samples"
    return None


def _episode_pairs(episodes):
    try:
        pairs = tuple(tuple(pair) for pair in episodes)
    except TypeError:
        return None
    if not pairs:
        return None
    if any(len(pair) != 2 or not all(_number(value) for value in pair) for pair in pairs):
        return None
    return pairs


def _decision(reference, positive, pairs):
    if positive[0] > reference[0] + TOLERANCE or positive[1] > reference[1]:
        return "reference", "held_presence_regression"
    if positive[2] > reference[2] + TOLERANCE:
        return "reference", "held_empty_duration_regression"
    if any(pos > ref + TOLERANCE for ref, pos in pairs):
        return "reference", "held_empty_episode_regression"
    if positive[0] < reference[0] - TOLERANCE or positive[2] < reference[2] - TOLERANCE:
        return "corrected_positive", "held_duration_improvement"
    return "reference", "held_duration_tie"


def select(reference, positive, empty_episodes, *, measurable):
    """Choose a duration improvement only without presence or episode harm.

    Reports accept normalized metrics or DurationReplay.summary field names.
    The caller's measurable flag attests that both held classes have observed
    time; time and unscored fields, when supplied, independently verify it.
    Missing or invalid validation preserves provisional automatic learning.
    """
    if not measurable:
        return Selection("corrected_positive", False, "missing_class_or_zero_time_validation")
    ref, pos = _metrics(reference), _metrics(positive)
    if ref is None or pos is None:
        return Selection("corrected_positive", False, "missing_or_invalid_validation_report")
    limitation = _limitation(reference) or _limitation(positive)
    if limitation:
        return Selection("corrected_positive", False, limitation)
    pairs = _episode_pairs(empty_episodes)
    if pairs is None:
        return Selection("corrected_positive", False, "missing_or_invalid_empty_episode_validation")
    policy, reason = _decision(ref, pos, pairs)
    return Selection(policy, True, reason)
