"""Shared error cost, display score and strict-improvement policy for all learners."""

import math

from .constants import AUTO_CLASS_CAP, MISSED_TIME_COST


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


def mass(row):
    """How much an observation counts. A human one counts 1; an automatic one its confidence
    times the number of samples it stands for (``collect_samples`` keeps a few samples of a long
    stretch)."""
    if len(row) < 4:
        return 1.0
    return row[3] * row[4] if len(row) > 4 else row[3]


def automatic_share(weight, human, automatic):
    """How much one class's automatic error rate counts beside its human rate.

    ``weight`` (the 0-1 setting) scales linearly up to ``AUTO_CLASS_CAP``: 0 = automatic
    evidence only breaks ties, 1 = it counts up to a quarter of the human evidence. Less
    automatic confidence mass than human mass scales it down further. A class with no human
    evidence takes the full ratio.
    """
    ratio = min(1.0, automatic / human) if human else 1.0
    return weight * AUTO_CLASS_CAP * ratio


def blended_cost(human_cost, shares, automatic_rates):
    """Primary learning cost: human cost plus the share-weighted automatic time cost
    (``automatic_rates`` = missed, false-active). Zero shares add nothing."""
    if not any(shares):
        return human_cost
    return human_cost + error_cost(
        *(share * rate for share, rate in zip(shares, automatic_rates, strict=True))
    )


def episode_share(weight, human, automatic):
    """Independent support controls influence; an unlabelled class retains its primary source."""
    if not automatic:
        return 0.0
    if not human:
        return 1.0
    return weight * AUTO_CLASS_CAP * automatic / (human + automatic)
