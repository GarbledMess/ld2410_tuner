"""Samples built from rows ``(time, {gate: energy}, label)``, for tests that state their
readings as dictionaries (production reads them from history: ``Samples.recorded``)."""

from array import array

import test_tuner  # noqa: F401  (registers tuner_under_test)
from tuner_under_test.rooms import attribution

MISSING = attribution.MISSING


def samples_of(rows):
    """The rows as ``Samples``; readings are whole numbers from 0 to 100."""
    keys = tuple(dict.fromkeys(key for _, gates, *_ in rows for key in gates))
    matrix = bytearray()
    for _, gates, *_ in rows:
        for key in keys:
            value = gates.get(key, MISSING)
            if value != MISSING and not (type(value) is int and 0 <= value <= 100):
                raise ValueError(f"Gate readings are whole numbers from 0 to 100, not {value}")
            matrix.append(value)
    return attribution.Samples(keys, array("d", (row[0] for row in rows)), bytes(matrix))


def attribute(rows, episode, base, radar_label_at):
    """Whether the radar saw ``episode``: its human label, then its baseline."""
    inside = samples_of(rows).between(*episode)
    return attribution._decide(inside, base, radar_label_at)[0]


def rows_of(rows):
    """The rows ``(time, {gate: energy}, label)`` as ``evidence.Rows`` (gates in every row)."""
    from tuner_under_test.rooms import evidence

    keys = list(rows[0][1]) if rows else []
    return evidence.Rows(
        [row[0] for row in rows],
        [bytes(row[1][key] for row in rows) for key in keys],
        {key: position for position, key in enumerate(keys)},
    )
