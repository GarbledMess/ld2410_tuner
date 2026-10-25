"""Readings kept as columns read row by row exactly as reading each value does.

Search spans, a radar's recorded rows, their iteration and the confidence time behind
duration scoring are each read here against a reference that reads one value at a time.
"""

import math
import random
from array import array
from types import SimpleNamespace

import test_tuner  # noqa: F401
from tuner_under_test.calibration import duration, event_timing, timing
from tuner_under_test.const import HISTORY_KEYS
from tuner_under_test.history import dayfile, daystore, spans
from tuner_under_test.rooms import evidence

START = 1_760_486_400.0


def day(seed, rows=500):
    rnd = random.Random(seed)
    times = array("d", sorted(START + rnd.uniform(0, 86_000) for _ in range(rows)))
    ends = array("d", (min(t + rnd.choice((1, 6, 40)), START + 86_400) for t in times))
    ends = array("d", (min(e, times[i + 1]) if i + 1 < rows else e for i, e in enumerate(ends)))
    columns = [bytes(rnd.choice((255, 0, 3, 40, 99)) for _ in range(rows)) for _ in range(18)]
    return dayfile.Day(START, START + 86_400, times, ends, columns, [])


def view(tmp_path, seeds):
    store = daystore.DayStore(tmp_path / "history")
    for offset, seed in enumerate(seeds):
        found = day(seed)
        shift = offset * 86_400
        shifted = dayfile.Day(
            found.start + shift,
            found.end + shift,
            array("d", (t + shift for t in found.times)),
            array("d", (t + shift for t in found.ends)),
            found.columns,
            [],
        )
        store.replace("radar", daystore.date_of(found.start + shift), shifted)
    return SimpleNamespace(_days=store, data={"devices": {"radar": {}}})


def test_search_spans_read_each_row_as_its_gates_do():
    from tuner_under_test.engine.columns import quantize

    for seed in range(6):
        found = day(seed)
        indices = sorted(random.Random(seed).sample(range(18), 5))
        for start, end in ((-math.inf, math.inf), (START + 900.5, START + 40_000.25)):
            expected = []
            for index in range(len(found.times)):
                low, high = max(start, found.times[index]), min(end, found.ends[index])
                if high > low:
                    values = bytes(quantize(found.columns[g], index, index + 1)[0] for g in indices)
                    expected.append((low, high, values))
            got = [
                (s.start, s.end, s.values) for s in spans._search_day(found, start, end, indices)
            ]
            assert got == expected


def test_recorded_rows_and_barriers_are_each_spans_readings(tmp_path):
    source = view(tmp_path, (1, 2, 3))
    for keys in (["g0_move", "g3_still", "g8_move"], list(HISTORY_KEYS)):
        indices = [HISTORY_KEYS.index(key) for key in keys]
        rows, barriers = evidence._recorded(source, "radar", keys, math.inf)
        times, ends, columns, blocked = [], [], [[] for _ in keys], set()
        for cell in spans.search_events(source, "radar", indices):
            if all(value == evidence.MISSING for value in cell.values):
                blocked.add(cell.start)
                continue
            times.append(cell.start)
            ends.append(cell.end)
            for column, value in zip(columns, cell.values, strict=True):
                column.append(value)
        assert list(rows.times) == times and list(rows.ends) == ends
        assert rows.columns == [bytes(column) for column in columns]
        assert barriers == sorted(blocked)


def test_rows_iterate_as_they_index(tmp_path):
    rows, _barriers = evidence._recorded(
        view(tmp_path, (4,)), "radar", ["g0_move", "g1_still"], math.inf
    )
    assert len(rows) > 100
    indexed = [rows[index] for index in range(len(rows))]
    iterated = list(rows)
    assert [(t, r.values, s) for t, r, s in iterated] == [(t, r.values, s) for t, r, s in indexed]
    empty = evidence.Rows(array("d", [1.0, 2.0]), [], {})
    assert [(t, r.values) for t, r, _s in empty] == [(1.0, b""), (2.0, b"")]


def test_confidence_time_and_event_windows_read_the_rows_times(tmp_path):
    rows, _barriers = evidence._recorded(view(tmp_path, (5,)), "radar", ["g0_move"], math.inf)
    tuples = list(rows)
    found = duration._confidence_time(rows)
    expected = duration.WeightedTime(
        [row[0] for row in tuples] + [rows.ends[-1]], [1.0] * len(tuples) + [0.0]
    )
    for t in (rows.times[0], rows.times[len(rows) // 2] + 0.5, rows.ends[-1], rows.ends[-1] + 9):
        assert found.at(t) == expected.at(t)
    times = array("d", (START + 6 * i for i in range(50)))  # Contiguous: one event window.
    contiguous = evidence.Rows(times, [bytes(50)], {"g0_move": 0}, ends=[t + 6 for t in times])
    window = event_timing.EventWindow(contiguous, 0, 0, 0)
    assert window.times == timing.sample_times(contiguous) == [row[0] for row in contiguous]
    sampled = [(1.0, None, "present", 0.5, 4), (3.0, None, "present", 0.25, 2)]  # Weighted.
    reference = duration.WeightedTime([1.0, 2.0], [2.0, 0.5])
    assert duration._confidence_time(sampled).at(3.0) == reference.at(3.0)
