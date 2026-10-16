"""Compare fixed gate patterns on common evidence without learning or writing gates."""

import math

from ..history import pace
from .constants import MIN_CLASS_SAMPLES

SCORE_MODEL = "weighted_time_v1"
# Increment whenever scoring, evidence selection, outlier or timing semantics change.
SCORER_VERSION = 2


@pace.background
def calculate(view, device_id, context):
    from .event_fitting import compare

    return compare(view, device_id, context)


def best_slots(patterns):
    eligible = {
        name: item
        for name, item in patterns.items()
        if name != "live" and item.get("applicable") and item.get("score") is not None
    }
    best = min((_rank(item) for item in eligible.values()), default=None)
    return [name for name, item in eligible.items() if _rank(item) == best]


def _rank(item):
    # A deliberate safety gate, independent of the automatic evidence weight setting:
    # automatic evidence only breaks ties here, so Automatic Apply needs a human-label gain.
    return tuple(item["ranking"])


def _source(label, human, automatic):
    samples, seconds = (
        ("present_samples", "present_seconds")
        if label == "present"
        else ("not_present_samples", "empty_seconds")
    )
    if human[samples] + automatic[samples] < MIN_CLASS_SAMPLES:
        return None
    for name, measured in (("human", human), ("automatic", automatic)):
        if measured["duration"][seconds] > 0:
            return name
    return None


def complete_thresholds(values, keys):
    return set(values) == set(keys) and all(_valid_threshold(value) for value in values.values())


def _valid_threshold(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 100
