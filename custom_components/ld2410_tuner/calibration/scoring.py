"""Shared error cost, display score and strict-improvement policy for all learners."""

import math

from .constants import MISSED_TIME_COST


def error_cost(missed_fraction, false_fraction):
    """Class-normalized time cost: missed presence matters more, never infinitely."""
    return 100 * (MISSED_TIME_COST * max(0.0, missed_fraction) + max(0.0, false_fraction))


def score_from_cost(cost):
    """Rounding must not turn a small measured error into a perfect score."""
    if not math.isfinite(cost) or cost < 0:
        raise ValueError("Score requires a finite, non-negative error cost")
    if cost == 0:
        return 100.0
    return min(99.99, round(max(0.0, 100.0 - cost), 2))


def improves(current, candidate):
    """Compare unrounded penalties, allowing for numerical integration noise."""
    return candidate["error_cost"] < current["error_cost"] - 1e-9
