"""Compact threshold inputs; callable from a standalone numerical worker.

Recorded byte columns are already in the required representation. Float columns retain
their raw precision in storage; only integer-threshold search inputs are rounded up.
The missing sentinel 255 must never be interpreted as an energy reading.
"""

from math import ceil, isnan

MISSING = 255


def percentage(value):
    """Whole-percent energy, preserving every integer threshold crossing.

    Apply at ingestion regardless of whether the device firmware rounded its own
    percentage. Legacy fractional recordings are still accepted by ``quantize``.
    """
    if value is None or not 0 <= value <= 100:
        return None
    return ceil(value)


def quantize(column, first, after):
    """Immutable search bytes for ``[first, after)`` of one recorded column.

    For every integer threshold, ``ceil(value) > threshold`` has the same truth value
    as ``value > threshold``. A native backend can implement this buffer contract without
    depending on history views, labels, HA objects or the caller's choice of deployment.
    """
    if not 0 <= first <= after <= len(column):
        raise ValueError("Invalid column extent")
    if isinstance(column, bytes):
        return column[first:after]
    return bytes(MISSING if isnan(value) else ceil(value) for value in column[first:after])
