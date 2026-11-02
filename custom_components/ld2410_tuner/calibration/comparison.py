"""Compare fixed gate patterns on common evidence without learning or writing gates."""

import math

from ..history import pace

SCORE_MODEL = "hybrid_time_episode_v1"
# Increment whenever scoring, evidence selection, outlier or timing semantics change.
SCORER_VERSION = 3
# The hybrid constants that decide a fixed-pattern score's fidelity (exact rows or six-second
# cells) and its row budget. A stored score is reused only under the same values.
FIDELITY = (
    "CELL_SECONDS",
    "WARMUP_SECONDS",
    "CHUNK_SECONDS",
    "ROW_BLOCK_ROWS",
    "EXACT_BUDGET",
    "MIN_EXACT",
    "MISSED_WEIGHT",
    "TOLERANCE",
    "AUTOMATIC_HORIZON_DAYS",
    "HUMAN_EVIDENCE_HOURS",
)


def fidelity():
    """The scorer identity a stored comparison score must match, read when asked."""
    from ..engine.hybrid import constants

    return {
        "model": SCORE_MODEL,
        "scorer_version": SCORER_VERSION,
        **{name.lower(): getattr(constants, name) for name in FIDELITY},
    }


@pace.background
def calculate(view, device_id, context):
    from ..engine.hybrid import facade

    return facade.compare(view, device_id, context)


def best_slots(patterns):
    eligible = {
        name: item
        for name, item in patterns.items()
        if name != "live" and item.get("applicable") and item.get("score") is not None
    }
    best = min((_rank(item) for item in eligible.values()), default=None)
    return [name for name, item in eligible.items() if _rank(item) == best]


def _rank(item):
    # The facade ranks the shared source-aware time-and-episode cost first;
    # episode counts and source diagnostics resolve ties consistently with Apply.
    return tuple(item["ranking"])


def complete_thresholds(values, keys):
    return set(values) == set(keys) and all(_valid_threshold(value) for value in values.values())


def _valid_threshold(value):
    return type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 100
