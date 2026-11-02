"""Conservative selection on later explicit episodes.

Hybrid held-out scores choose on the common time-and-episode cost; saved duration-only
diagnostics choose on durations. Either way a policy that adds missed presence, false-active
time, mostly-missed or failed episodes, or harm in any one empty episode is not chosen."""

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


# Held-out hybrid scores add the common time-and-episode cost and these episode counts.
COST = "cost"
PRESENCE_COUNTS = ("mostly_missed_episodes",)
EMPTY_COUNTS = ("empty_periods_failed", "empty_periods_mostly_false_active")


def _count(value):
    return _number(value) and float(value).is_integer()


def _metrics(report):
    """``(missed, missed episodes, false, hybrid)``; ``hybrid`` is None for a duration-only
    report, else ``(cost, presence counts, empty counts)``."""
    if not isinstance(report, Mapping):
        return None
    values = (
        report.get("missed_seconds"),
        report.get("missed_episodes", report.get("missed_presence_episodes")),
        report.get("false_seconds", report.get("false_positive_seconds")),
    )
    if not all(_number(value) for value in values) or not _count(values[1]):
        return None
    if COST not in report:
        return (*values, None)
    presence = tuple(report.get(key) for key in PRESENCE_COUNTS)
    empty = tuple(report.get(key) for key in EMPTY_COUNTS)
    if not _number(report[COST]) or not all(_count(value) for value in (*presence, *empty)):
        return None
    return (*values, (report[COST], presence, empty))


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


def _more(positive, reference):
    return any(pos > ref for pos, ref in zip(positive, reference, strict=True))


def _harm(reference, positive, pairs):
    """The first held-out harm the positive policy adds, whatever its total cost."""
    ref_hybrid, pos_hybrid = reference[3], positive[3]
    if positive[0] > reference[0] + TOLERANCE or positive[1] > reference[1]:
        return "held_presence_regression"
    if ref_hybrid and _more(pos_hybrid[1], ref_hybrid[1]):
        return "held_presence_regression"
    if positive[2] > reference[2] + TOLERANCE:
        return "held_empty_duration_regression"
    if ref_hybrid and _more(pos_hybrid[2], ref_hybrid[2]):
        return "held_empty_episode_regression"
    if any(pos > ref + TOLERANCE for ref, pos in pairs):
        return "held_empty_episode_regression"
    return None


def _decision(reference, positive, pairs):
    harm = _harm(reference, positive, pairs)
    if harm:
        return "reference", harm
    if reference[3]:
        if positive[3][0] < reference[3][0] - TOLERANCE:
            return "corrected_positive", "held_cost_improvement"
        return "reference", "held_cost_tie"
    if positive[0] < reference[0] - TOLERANCE or positive[2] < reference[2] - TOLERANCE:
        return "corrected_positive", "held_duration_improvement"
    return "reference", "held_duration_tie"


def select(reference, positive, empty_episodes, *, measurable):
    """Choose a cost (or, for duration-only reports, duration) improvement only without
    presence or episode harm.

    Reports accept hybrid held-out scores (durations plus ``cost`` and episode counts),
    normalized duration metrics or DurationReplay.summary field names.
    The caller's measurable flag attests that both held classes have observed
    time; time and unscored fields, when supplied, independently verify it.
    Missing or invalid validation preserves provisional automatic learning.
    """
    if not measurable:
        return Selection("corrected_positive", False, "missing_class_or_zero_time_validation")
    ref, pos = _metrics(reference), _metrics(positive)
    if ref is None or pos is None or (ref[3] is None) != (pos[3] is None):
        return Selection("corrected_positive", False, "missing_or_invalid_validation_report")
    limitation = _limitation(reference) or _limitation(positive)
    if limitation:
        return Selection("corrected_positive", False, limitation)
    pairs = _episode_pairs(empty_episodes)
    if pairs is None:
        return Selection("corrected_positive", False, "missing_or_invalid_empty_episode_validation")
    policy, reason = _decision(ref, pos, pairs)
    return Selection(policy, True, reason)
