"""Bulk readers of rows and cells give exactly what a walk over each row or cell gives."""

import random
import re
from array import array

import pytest
import test_tuner  # noqa: F401
from tuner_under_test.const import HISTORY_KEYS
from tuner_under_test.engine.hybrid import anomaly, digest, replay, reports
from tuner_under_test.engine.hybrid.diagnostics import SampleSlots
from tuner_under_test.engine.hybrid.types import RowBlock


def _hits_walk(mask, times, ends, gaps):
    data = mask.to_bytes(len(times), "little")
    cuts = (0, *gaps, len(times))
    found = []
    for first, stop in zip(cuts, cuts[1:], strict=False):
        for match in re.finditer(rb"\x01+", data[first:stop]):
            found.append((times[first + match.start()], ends[first + match.end() - 1]))
    return found


@pytest.mark.parametrize("seed", range(40))
def test_hits_equal_a_walk_over_each_run(seed):
    rng = random.Random(seed)
    n = rng.randint(1, 60)
    density = rng.choice((0.0, 0.1, 0.5, 0.9, 1.0))
    data = bytes(rng.random() < density for _ in range(n))
    times, ends, at, gaps = array("d"), array("d"), 0.0, []
    for i in range(n):
        if i and rng.random() < 0.15:
            at += rng.uniform(0.5, 9)
            gaps.append(i)
        times.append(at)
        at += rng.uniform(0.05, 6)
        ends.append(at)
    mask = int.from_bytes(data, "little")
    assert replay.hits(mask, times, ends, gaps) == _hits_walk(mask, times, ends, gaps)


def _reduce_walk(folder, block, first, after):
    lo, hi = block.times[first], block.ends[after - 1]
    folder.coverage(lo, hi)
    for k in range(digest.cell_floor(lo, 6), digest.cell_ceil(hi, 6)):
        a = digest.bisect_right(block.ends, k * 6, first, after)
        b = digest.bisect_left(block.times, (k + 1) * 6, a, after)
        cell = k - folder.k0
        for c in folder.selected:
            value = max(block.columns[c][a:b])
            old = folder.columns[c][cell]
            folder.columns[c][cell] = value if old == digest.MISSING else max(old, value)


def _irregular_blocks(rng, base):
    blocks, at = [], base + rng.uniform(0, 20)
    for _ in range(3):
        times, ends = array("d"), array("d")
        for _ in range(rng.randint(40, 300)):
            if rng.random() < 0.02:
                at += rng.uniform(1, 30)
            times.append(at)
            at += rng.choice((0.25, 0.5, rng.uniform(0.05, 0.9), rng.uniform(5.9, 6.1), 6.0))
            ends.append(at)
        columns = tuple(
            bytes(rng.choice((3, 40, 90, 100, 255 if rng.random() < 0.01 else 7)) for _ in times)
            for _ in range(18)
        )
        blocks.append(RowBlock(times, ends, columns))
    return blocks


@pytest.mark.parametrize("seed", range(12))
def test_day_folding_equals_a_walk_over_each_cell(seed, monkeypatch):
    rng = random.Random(seed)
    channels = rng.choice(((0,), (0, 1, 2), tuple(range(18))))
    base = 1_790_000_000.37 // 86400 * 86400
    blocks = _irregular_blocks(rng, base)
    found = digest.fold_day(iter(blocks), base, base + 86400, channels)
    with monkeypatch.context() as patched:
        patched.setattr(digest, "_reduce_cells", _reduce_walk)
        expected = digest.fold_day(iter(blocks), base, base + 86400, channels)
    assert found.channels == expected.channels
    assert found.cov == expected.cov
    assert list(found.segments()) == list(expected.segments())


def _run_column(rng):
    lengths = array("I", (rng.choice((1, 1, 1, 2, 5)) for _ in range(rng.randint(1, 80))))
    values = bytes(rng.choice((0, 7, 40, 100, 255)) for _ in lengths)
    ends = array("Q", anomaly.accumulate(lengths))
    return anomaly._RunColumn(values, lengths, ends)


def _flagged(rng, column, side):
    size = column.ends[-1]
    edges = sorted(rng.randint(0, size) for _ in range(6))
    return [(side, a, b, 0, 0) for a, b in zip(edges[::2], edges[1::2], strict=True) if b > a]


@pytest.mark.parametrize("seed", range(20))
def test_dropped_cells_equal_a_histogram_of_each_flagged_range(seed):
    rng = random.Random(seed)
    columns = _run_column(rng), _run_column(rng)
    flagged = _flagged(rng, columns[0], 0) + _flagged(rng, columns[1], 1)
    found = anomaly._dropped(columns, flagged)
    for side in (0, 1):
        expected = {}
        for s, lo, hi, _, _ in flagged:
            if s == side:
                for value, count in columns[side][lo:hi].items():
                    expected[value] = expected.get(value, 0) + count
        assert {v: n for v, n in found[side].items() if v <= 100} == expected


def test_dropped_reads_each_column_once_however_many_windows_are_flagged(monkeypatch):
    rng = random.Random(3)
    columns = _run_column(rng), _run_column(rng)
    size = columns[0].ends[-1]
    flagged = [(0, i, i + 1, 0, 0) for i in range(0, size - 1, 2)]
    calls = []
    original = anomaly._per_cell

    def counted(*args):
        calls.append(1)
        return original(*args)

    monkeypatch.setattr(anomaly, "_per_cell", counted)
    anomaly._dropped(columns, flagged)
    assert len(calls) <= 2


@pytest.mark.parametrize("seed", range(12))
def test_decisions_expand_to_a_cell_per_run_length(seed):
    rng = random.Random(seed)
    column = _run_column(rng)
    above = column.translate(replay.TABLES[20])
    cells = bytearray()
    for value, length in zip(column.values, column.lengths, strict=True):
        cells += bytes((replay.TABLES[20][value],)) * length
    assert bytes(above._expanded()) == bytes(cells)


def _sample_rows(slots):
    return [slots[i] for i in range(len(slots))]


@pytest.mark.parametrize("seed", range(20))
def test_exclusive_presence_from_columns_equals_a_walk_over_samples(seed):
    rng = random.Random(seed)
    channels = (0, 1, 4, 7)
    samples = [
        (i * 6.0, "human", bytes(rng.choice((0, 12, 40, 60, 100)) for _ in channels))
        for i in range(rng.randint(0, 60))
    ]
    slots = SampleSlots(channels, samples)
    thresholds = {HISTORY_KEYS[c]: rng.choice((10, 40, 59, 100)) for c in channels}
    thresholds[HISTORY_KEYS[2]] = 5  # A gate with no readings never detects.
    walked = {"samples": {"present": _sample_rows(slots)}, "thresholds": thresholds}
    columns = {"samples": {"present": slots}, "thresholds": thresholds}
    for key in [*thresholds, HISTORY_KEYS[9]]:
        limit = thresholds.get(key, 40)
        assert reports._exclusive(columns, key, limit) == reports._exclusive(walked, key, limit)
        assert reports._weakest(columns, key, limit) == reports._weakest(walked, key, limit)
