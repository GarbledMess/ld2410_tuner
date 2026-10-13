"""Which automatic guesses count as evidence: a radar's guess with a calibrated confidence.

Recorded confidence is the age of the radar's newest reference, not a probability. Evidence
reads it through ``trusted`` (a class-specific calibration, ``constants.TRUST_*``) and, for a
present guess, through the radar's quiet baseline: the ceiling of each gate's Empty-labelled
readings, above which at least one gate must read. ``guesses.evidence`` and ``guesses.counted``
apply both; this module holds the pure parts.
"""

from __future__ import annotations

from bisect import bisect_right

from ..calibration.constants import (
    QUIET_MARGIN,
    QUIET_QUANTILE,
    TRUST_NOT_PRESENT,
    TRUST_PRESENT,
)
from ..const import HISTORY_KEYS
from ..presence.inference import MIN_REFERENCE, quantile

BOUNDS = tuple(bound for bound, _ in TRUST_PRESENT)
NO_CEILING = 100  # A gate with no baseline cannot read above it.


def trusted(state, confidence):
    """The probability that a radar's ``state`` guess of recorded ``confidence`` is right."""
    if state == "not_present":
        return TRUST_NOT_PRESENT
    return TRUST_PRESENT[bisect_right(BOUNDS, confidence) - 1][1]


def ceilings(histograms):
    """Per gate of ``HISTORY_KEYS``, the readings of the radar's Empty labels reach no higher
    than this (``NO_CEILING`` where it has too few). None when no gate has a baseline, so there
    is nothing to veto with. ``histograms`` is the device's labelled histograms."""
    found = []
    for key in HISTORY_KEYS:
        empty = ((histograms or {}).get(key) or {}).get("not_present") or ()
        enough = sum(empty) >= MIN_REFERENCE
        found.append(quantile(empty, QUIET_QUANTILE) + QUIET_MARGIN if enough else NO_CEILING)
    return tuple(found) if any(value != NO_CEILING for value in found) else None


def quiet(row, limits):
    """Whether no gate of ``row`` (energies, above 100 unread) reads above its ceiling."""
    return not any(limit < value <= 100 for value, limit in zip(row, limits, strict=False))
