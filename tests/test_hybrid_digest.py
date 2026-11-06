"""Synthetic checks for streaming physical cell geometry."""

import random
from array import array

import pytest
import test_tuner  # noqa: F401
from tuner_under_test.engine.hybrid.digest import fold_day
from tuner_under_test.engine.hybrid.types import RowBlock


def test_full_cells_are_folded_in_column_batches(monkeypatch):
    from tuner_under_test.engine.hybrid import digest

    rows = [(i * 6, (i + 1) * 6, [i % 101] * 18) for i in range(1000)]
    rows[10:11] = [(60, 60.125, [90] * 18), (60.125, 66, [30] * 18)]
    rows[200] = (rows[200][0], rows[200][1], [255] * 18)
    calls = []
    original = digest._Folder.row

    def counted(folder, *args):
        calls.append(1)
        return original(folder, *args)

    monkeypatch.setattr(digest._Folder, "row", counted)
    result = fold_day([block(rows[:400]), block(rows[400:])], 0, 6000, tuple(range(18)))
    expected = bytearray(i % 101 for i in range(1000))
    expected[10], expected[199] = 90, 255
    assert result.channels == (bytes(expected),) * 18
    assert result.cov == bytes([1]) * 199 + b"\x00" + bytes([1]) * 800
    assert len(calls) <= 2


def test_contiguous_coverage_is_marked_in_bulk():
    from tuner_under_test.engine.hybrid import digest

    class Coverage(bytearray):
        writes = 0

        def __setitem__(self, key, value):
            self.writes += 1
            super().__setitem__(key, value)

    folder = digest._Folder(0, 86400, (0,))
    folder.cov = Coverage(14400)
    folder.coverage(0.125, 86399.875)
    folder.flush()
    assert folder.cov == b"\x02" + b"\x01" * 14398 + b"\x02"
    assert dict(folder.partial) == {0: ((0.125, 6),), 14399: ((86394, 86399.875),)}
    assert folder.cov.writes <= 3


def block(rows):
    return RowBlock(
        array("d", (r[0] for r in rows)),
        array("d", (r[1] for r in rows)),
        tuple(bytes(r[2][c] for r in rows) for c in range(18)),
    )


def test_digest_matches_brute_force():
    rng = random.Random(32)
    rows = []
    t = 0.0
    for _ in range(300):
        t += rng.choice([0, 0.125, 1])
        end = t + rng.choice([0.125, 0.5, 6])
        values = [rng.randrange(101) for _ in range(18)]
        if rng.random() < 0.1:
            values[1] = 255
        rows.append((t, end, values))
        t = end
    d = fold_day([block(rows)], 0, 1200, (0, 1))
    for k in range(d.n):
        usable = [
            r
            for r in rows
            if r[0] < (k + 1) * 6 and r[1] > k * 6 and all(r[2][c] != 255 for c in (0, 1))
        ]
        for c in (0, 1):
            assert d.channels[c][k] == max((r[2][c] for r in usable), default=255)
        seconds = sum(max(0, min(r[1], (k + 1) * 6) - max(r[0], k * 6)) for r in usable)
        assert d.cov[k] == (0 if seconds == 0 else 1 if seconds == 6 else 2)


def test_epoch_boundary_run_does_not_leak_into_next_cell():
    edge = float(1790000000 // 6 * 6)
    d = fold_day(
        [block([(edge - 9, edge, [90] * 18), (edge, edge + 6, [0] * 18)])],
        edge - 12,
        edge + 6,
        (0,),
    )
    assert d.channels[0] == bytes([90, 90, 0])
    assert d.cov == bytes([2, 1, 1])
    assert list(d.segments()) == [(edge - 9, edge + 6)]


def test_strict_crossing_and_missing_never_detects():
    rows = [(i * 6, (i + 1) * 6, [v] * 18) for i, v in enumerate([0, 40, 41, 255])]
    d = fold_day([block(rows)], 0, 24, (0,))
    assert [v != 255 and v > 40 for v in d.channels[0]] == [False, False, True, False]
    assert d.cov == bytes([1, 1, 1, 0])


def test_unconfigured_channels_are_unselected():
    d = fold_day([block([(0, 6, [40] + [255] * 17)])], 0, 6, (0,))
    assert d.cov == b"\x01"
    assert d.channels[1] == b"\xff"
    assert fold_day([block([(0, 6, [40] + [255] * 17)])], 0, 6, (0, 1)).cov == b"\x00"


def test_fold_is_identical_across_4096_row_blocks():
    rows = [(i / 1024, (i + 1) / 1024, [i % 101] * 18) for i in range(9000)]
    a = fold_day([block(rows)], 0, 12, (0, 1))
    b = fold_day((block(rows[i : i + 4096]) for i in range(0, len(rows), 4096)), 0, 12, (0, 1))
    assert a.channels == b.channels
    assert a.cov == b.cov
    assert dict(a.partial) == dict(b.partial)


@pytest.mark.parametrize("start,end,channels", [(0, 0, (0,)), (0, 6, (18,)), (0, 6, ())])
def test_invalid_fold_request(start, end, channels):
    with pytest.raises(ValueError):
        fold_day([], start, end, channels)


@pytest.mark.parametrize("first,after", [(0, 10000), (1, 9999), (2000, 2001)])
def test_contiguous_batches_find_gaps_without_per_row_python_steps(first, after):
    import sys

    from tuner_under_test.engine.hybrid import digest

    rows = [(i * 6, (i + 1) * 6, [20] * 18) for i in range(10000)]
    rows[2500] = (15001, 15006, [20] * 18)
    rows[7500] = (45000, 45005, [20] * 18)
    source = block(rows)
    expected, start = [], first
    for i in range(first + 1, after):
        if source.times[i] != source.ends[i - 1]:
            expected.append((start, i))
            start = i
    expected.append((start, after))
    steps = []

    def trace(frame, event, _argument):
        if frame.f_code is digest._contiguous.__code__ and event == "line":
            steps.append(1)
        return trace

    sys.settrace(trace)
    try:
        actual = list(digest._contiguous(source, first, after))
    finally:
        sys.settrace(None)
    assert actual == expected
    assert len(steps) < 100
