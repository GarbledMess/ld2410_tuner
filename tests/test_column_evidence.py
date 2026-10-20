"""Selected column evidence agrees with the physical-span oracle."""

import math
import random
from unittest.mock import patch

import pytest
import test_tuner  # noqa: F401
from test_event_quality import view
from tuner_under_test.const import HISTORY_KEYS
from tuner_under_test.history import dayfile, spans
from tuner_under_test.rooms import evidence


def expected(source, keys, end):
    indices = [HISTORY_KEYS.index(key) for key in keys]
    rows, barriers = [], []
    for cell in spans.events(source, "any-name", end=end):
        values = [cell.values[i] for i in indices]
        if all(value is None for value in values):
            barriers.append(cell.start)
        else:
            rows.append(
                (cell.start, cell.end, bytes(255 if v is None else math.ceil(v) for v in values))
            )
    return rows, sorted(set(barriers))


@pytest.mark.parametrize("keys", [[], [HISTORY_KEYS[17]], list(HISTORY_KEYS)])
def test_column_evidence_keeps_gaps_missing_fractional_readings_and_cutoffs(keys):
    rng = random.Random(2410)
    events = [
        (i, tuple(rng.choice([None, 0, 10.01, 99.9]) for _ in HISTORY_KEYS)) for i in range(30)
    ]
    source = view([(events[:10], 10), (events[20:], 30)], [])
    source._physical_history_start = 2.25
    source._attribution_until = 28.5
    source._learning_windows = {"any-name": [(3.75, 8.5), (21.5, 29)]}
    oracle, barriers = expected(source, keys, 27.75)
    with patch.object(dayfile.Day, "vector", side_effect=AssertionError("full vector rebuilt")):
        actual, missing = evidence._recorded(source, "any-name", keys, 27.75)
    assert list(zip(actual.times, actual.ends, strict=True)) == [(a, b) for a, b, _ in oracle]
    assert [bytes(col[i] for col in actual.columns) for i in range(len(actual))] == [
        values for _, _, values in oracle
    ]
    assert missing == barriers


def test_column_evidence_keeps_integer_missing_barriers():
    source = view([([(0, (42,) + (None,) * 17), (4, (None,) * 18)], 10)], [])
    oracle, barriers = expected(source, [HISTORY_KEYS[0]], 10)
    with patch.object(dayfile.Day, "vector", side_effect=AssertionError("full vector rebuilt")):
        actual, missing = evidence._recorded(source, "any-name", [HISTORY_KEYS[0]], 10)
    assert [(a, b) for a, b, _ in oracle] == list(zip(actual.times, actual.ends, strict=True))
    assert actual.column(HISTORY_KEYS[0]) == bytes([42])
    assert missing == barriers == [4]
