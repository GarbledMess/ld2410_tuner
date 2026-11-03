"""Physical rows survive cell caching without raw decoding or whole-history arrays."""

import random
from array import array
from unittest.mock import patch

import pytest
import test_tuner  # noqa: F401
from test_cell_cache import DAY, fitting
from test_hybrid_digest import block
from tuner_under_test.engine.hybrid.constants import CELL_CACHE_VERSION
from tuner_under_test.engine.hybrid.digest import fold_day
from tuner_under_test.engine.hybrid.physical_index import PhysicalRows, physical_rows
from tuner_under_test.history.fit_rows import CapturedRowSource


def vector(*values):
    return list(values) + [255] * (18 - len(values))


def test_usable_rows_coalesce_only_contiguous_identical_selected_vectors():
    rows = [
        (0, 1, vector(40, 9)),
        (1, 2, vector(40, 10)),  # unselected channel 1 changes: same selected vector
        (2, 3, vector(41, 10)),
        (3, 3, vector(90, 10)),  # zero duration is never usable
        (3.5, 4, vector(41, 10)),  # coverage break keeps a separate row
        (4, 5, vector(255, 10)),  # missing selected reading is unusable
        (5, 6, vector(41, 10)),
    ]
    digest = fold_day([block(rows)], 0, 6, (0,))
    physical = digest.physical
    assert physical.count == 4
    times, ends, columns = physical.window(0, 6)
    assert list(times) == [0, 2, 3.5, 5]
    assert list(ends) == [2, 3, 4, 6]
    assert columns == {0: bytes([40, 41, 41, 41])}
    assert physical.rows_in(0, 6) == 4
    assert physical.rows_in(2.5, 3.6) == 2
    assert physical.rows_in(6, 9) == 0


def test_window_clips_partial_rows_to_the_requested_interval():
    physical = fold_day([block([(0, 4, vector(40)), (4, 8, vector(50))])], 0, 12, (0,)).physical
    times, ends, columns = physical.window(1, 5)
    assert (list(times), list(ends), columns) == ([1, 4], [4, 5], {0: bytes([40, 50])})
    assert physical.window(8, 12)[0] == array("d")


def test_millisecond_and_float_geometry_round_trip_exactly():
    rng = random.Random(77)
    for scale in (1000, 7):  # 7 forces the raw-double fallback
        rows, t = [], 1_790_000_000.0
        for _ in range(500):
            end = t + rng.randrange(1, 900) / scale
            rows.append((t, end, vector(rng.randrange(101), rng.choice([20, 255]))))
            t = end + rng.choice([0, 0, 0, 1 / scale])
        physical = physical_rows([block(rows)], rows[0][0], rows[-1][1], (0, 1))
        again = PhysicalRows.decode((0, 1), physical.encode())
        assert again.window(-1e12, 1e12) == physical.window(-1e12, 1e12)
        assert again.count == physical.count


@pytest.mark.parametrize("damage", ["count", "order", "energy"])
def test_corrupt_physical_section_is_rejected(damage):
    physical = fold_day([block([(0, 1, vector(40)), (1, 2, vector(50))])], 0, 6, (0,)).physical
    encoded = bytearray(physical.encode())
    if damage == "count":
        encoded[0] += 1
    rebuilt = PhysicalRows.decode((0,), bytes(encoded))
    if damage == "order":
        bad = PhysicalRows((0,), array("d", [1, 0]), array("d", [2, 1]), {0: bytes([1, 2])})
        rebuilt = PhysicalRows.decode((0,), bad.encode())
    if damage == "energy":
        bad = PhysicalRows((0,), array("d", [0]), array("d", [1]), {0: bytes([255])})
        rebuilt = PhysicalRows.decode((0,), bad.encode())
    with pytest.raises(ValueError):
        rebuilt.window(0, 6)


def test_physical_index_warm_cache_roundtrip_is_lazy(tmp_path):
    fit, _, _ = fitting(tmp_path)
    first = fit.digest("synthetic", DAY, (0,))
    second = fit.digest("synthetic", DAY, (0,))
    assert not second.physical.decoded
    assert first.physical.rows_in(0, 86400) == second.physical.rows_in(0, 86400)
    assert first.physical.window(0, 86400) == second.physical.window(0, 86400)


def test_cell_sidecar_version_includes_physical_rows(tmp_path):
    fit, _, path = fitting(tmp_path)
    fit.digest("synthetic", DAY, (0,))
    assert CELL_CACHE_VERSION == 4
    assert path.with_suffix(".cell").read_bytes()[9] == CELL_CACHE_VERSION


def test_warm_rows_bypass_raw_decoder_and_budget_count_is_unchanged(tmp_path):
    fit, days, _ = fitting(tmp_path)
    rows = CapturedRowSource(fit.view, "synthetic", (0,))
    cold_count = rows.rows_in(0, 86400)
    cold = rows.window(0, 86400)
    rows = CapturedRowSource(fit.view, "synthetic", (0,))
    with patch.object(days, "open_columns", side_effect=AssertionError("decoded warm rows")):
        warm_count = rows.rows_in(0, 86400)
        warm = rows.window(0, 86400)
    assert cold_count == warm_count == 3
    assert cold == warm
    assert list(warm[1]) == [1, 2, 3]


def test_live_rows_are_folded_once_per_source(tmp_path):
    fit, days, _ = fitting(tmp_path)
    days.entries[("synthetic", DAY)]["persistent"] = False
    rows = CapturedRowSource(fit.view, "synthetic", (0,))
    original = days.open_columns
    with patch.object(days, "open_columns", wraps=original) as reads:
        rows.rows_in(0, 86400)
        rows.window(0, 3600)
        rows.window(3600, 86400)
    assert reads.call_count == 1


def test_block_wholly_continuing_previous_row_adds_no_row():
    # Regression: a block whose every row continued the previous block's row appended
    # an extra end, shifting every later row's end and losing covered time.
    blocks = [block([(6 * i, 6 * (i + 1), vector(40))]) for i in range(3)]
    blocks.append(block([(18, 24, vector(70))]))
    physical = physical_rows(blocks, 0, 24, (0,))
    times, ends, columns = physical.window(0, 24)
    assert list(times) == [0, 18]
    assert list(ends) == [18, 24]
    assert columns == {0: bytes([40, 70])}


def _dense_blocks(rows, size=97):
    """Never-coalescing rows: every selected channel alternates on every row."""
    from tuner_under_test.engine.hybrid.types import RowBlock

    for at in range(0, rows, size):
        stop = min(rows, at + size)
        column = bytes((i % 2) * 40 for i in range(at, stop))
        yield RowBlock(
            array("d", (i / 10 for i in range(at, stop))),
            array("d", ((i + 1) / 10 for i in range(at, stop))),
            (column,) * 18,
        )


def test_building_a_dense_day_keeps_at_most_one_block_of_rows_resident(monkeypatch):
    from tuner_under_test.engine.hybrid import physical_index

    monkeypatch.setattr(physical_index, "PHYSICAL_BLOCK_ROWS", 64)
    builder = physical_index.PhysicalBuilder(tuple(range(18)), 0, 86400)
    pending = []
    for part in _dense_blocks(2000):
        builder.add(part)
        pending.append(len(builder.times))
    rows = builder.finish()
    assert max(pending) <= 64
    assert rows.count == 2000 and len(rows._blocks()) == -(-2000 // 64)
    times, ends, columns = rows.window(0, 1e9)
    assert list(times) == [i / 10 for i in range(2000)]
    assert columns[3] == bytes((i % 2) * 40 for i in range(2000))
    assert rows.rows_in(10, 20.05) == len(rows.window(10, 20.05)[0]) == 101


def test_blocked_rows_equal_one_block_for_windows_and_counts(monkeypatch):
    from tuner_under_test.engine.hybrid import physical_index

    rng = random.Random(15)
    rows, t = [], 100.0
    for _ in range(700):
        end = t + rng.randrange(1, 9) / 4
        rows.append((t, end, vector(rng.choice([40, 41]))))
        t = end + rng.choice([0, 0, 0.25])
    whole = physical_rows([block(rows)], 0, 1e6, (0,))
    monkeypatch.setattr(physical_index, "PHYSICAL_BLOCK_ROWS", 7)
    blocked = physical_rows([block(rows[i : i + 50]) for i in range(0, 700, 50)], 0, 1e6, (0,))
    again = PhysicalRows.decode((0,), blocked.encode())
    assert len(blocked._blocks()) > 20
    for _ in range(200):
        lo = rng.uniform(90, t + 10)
        hi = lo + rng.choice([0, 0.1, 3, 40, 500])
        for other in (blocked, again):
            assert other.window(lo, hi) == whole.window(lo, hi)
            assert other.rows_in(lo, hi) == whole.rows_in(lo, hi)
    assert again.geometry() == whole.geometry()


def test_reading_a_section_spools_it_and_decodes_only_overlapping_blocks(monkeypatch):
    import io

    from tuner_under_test.engine.hybrid import physical_index

    monkeypatch.setattr(physical_index, "PHYSICAL_BLOCK_ROWS", 64)
    monkeypatch.setattr(physical_index, "SPOOL_BYTES", 512)
    monkeypatch.setattr(physical_index, "COPY_BYTES", 100)
    source = physical_rows(_dense_blocks(2000), 0, 86400, tuple(range(18)))
    stream = io.BytesIO(b"x" + source.encode() + b"tail")
    stream.read(1)
    rows = PhysicalRows.read(stream, source.encoded_size(), tuple(range(18)))
    assert stream.read() == b"tail"
    assert rows._spool._rolled
    decoded = []
    original = physical_index._decode_block

    def counted(*args):
        decoded.append(args[1])
        return original(*args)

    monkeypatch.setattr(physical_index, "_decode_block", counted)
    rows.window(64.05, 70)
    assert len(decoded) == 1
    assert rows.rows_in(0, 1e9) == 2000 and len(decoded) == 1


def test_millisecond_decode_constructs_double_arrays_from_sized_exact_values(monkeypatch):
    from itertools import accumulate

    from tuner_under_test.engine.hybrid import physical_index

    steps = array("q", [1_790_000_000_001, 1, 999, 1234567, -(2**62)])
    lengths = array("q", [1, 1000, 123, 0, 2**62])
    raw = physical_index._native(steps).tobytes()
    raw += physical_index._native(lengths).tobytes()
    starts = list(accumulate(steps))
    expected = (
        array("d", [value / 1000 for value in starts]),
        array(
            "d", [(value + length) / 1000 for value, length in zip(starts, lengths, strict=True)]
        ),
    )
    unsized_double_arrays = []

    def recorded_array(code, values):
        if code == "d" and not hasattr(values, "__len__"):
            unsized_double_arrays.append(code)
        return array(code, values)

    monkeypatch.setattr(physical_index, "array", recorded_array)
    assert physical_index._geometry(raw, 1) == expected
    assert unsized_double_arrays == []


@pytest.mark.parametrize("first", [1_790_000_000_000, -1_790_000_000_000, 2**50 - 10000])
def test_regular_dyadic_milliseconds_decode_without_per_row_division(monkeypatch, first):
    from tuner_under_test.engine.hybrid import physical_index

    # Keep the first timestamp at an integer second, including near the safe bound.
    first -= first % 1000
    steps = [first, *([500] * 9)]
    lengths = [125] * 10
    _assert_geometry_divisions(monkeypatch, physical_index, steps, lengths, 0)


def test_regular_geometry_matches_integer_division_across_full_blocks():
    from itertools import accumulate

    from tuner_under_test.engine.hybrid import physical_index

    rng = random.Random(2410)
    for _ in range(20):
        first = rng.randrange(-(2**48), 2**48) // 1000 * 1000
        step, length = 125 * rng.randrange(1, 10000), 125 * rng.randrange(10000)
        steps = array("q", [first, *([step] * (physical_index.PHYSICAL_BLOCK_ROWS - 1))])
        lengths = array("q", [length] * len(steps))
        starts = list(accumulate(steps))
        assert physical_index._regular_geometry(steps, lengths) == (
            array("d", [value / 1000 for value in starts]),
            array("d", [(value + length) / 1000 for value in starts]),
        )


@pytest.mark.parametrize(
    ("steps", "lengths"),
    [
        ([1000, 126, 126], [125, 125, 125]),
        ([1001, 500, 500], [125, 125, 125]),
        ([1000, 500, 1000], [125, 125, 125]),
        ([1000, 500, 500], [125, 126, 125]),
        ([2**50 + 376, 500, 500], [125, 125, 125]),
        ([1000, -500, -500], [125, 125, 125]),
        ([1000, 500, 500], [-125, -125, -125]),
        ([1000], [125]),
        ([], []),
    ],
)
def test_nonregular_milliseconds_keep_exact_integer_division(monkeypatch, steps, lengths):
    from tuner_under_test.engine.hybrid import physical_index

    _assert_geometry_divisions(monkeypatch, physical_index, steps, lengths, 2 * len(steps))


def _assert_geometry_divisions(monkeypatch, module, steps, lengths, divisions):
    from itertools import accumulate
    from operator import truediv

    starts = list(accumulate(steps))
    expected = (
        array("d", [value / 1000 for value in starts]),
        array(
            "d", [(value + length) / 1000 for value, length in zip(starts, lengths, strict=True)]
        ),
    )
    calls = []

    def counted(left, right):
        calls.append(1)
        return truediv(left, right)

    raw = module._native(array("q", steps)).tobytes()
    raw += module._native(array("q", lengths)).tobytes()
    monkeypatch.setattr(module, "truediv", counted)
    assert module._geometry(raw, 1) == expected
    assert len(calls) == divisions


def test_regular_geometry_checks_uniformity_without_python_row_iteration():
    from tuner_under_test.engine.hybrid import physical_index

    class Column(array):
        def __getitem__(self, key):
            result = super().__getitem__(key)
            return Column(self.typecode, result) if isinstance(key, slice) else result

        def __iter__(self):
            raise AssertionError("uniform geometry iterated every physical row")

    starts, stops = physical_index._regular_geometry(
        Column("q", [1000, 500, 500, 500]), Column("q", [500] * 4)
    )
    assert starts == array("d", [1, 1.5, 2, 2.5])
    assert stops == array("d", [1.5, 2, 2.5, 3])


def test_physical_validation_does_not_translate_entire_energy_columns():
    from tuner_under_test.engine.hybrid.physical_index import _validate

    class Column(bytes):
        def translate(self, table):
            raise AssertionError("energy validation allocated a translated column")

    _validate([0.0, 1.0], [1.0, 2.0], {0: Column([0, 100])})
    with pytest.raises(ValueError, match="energy"):
        _validate([0.0, 1.0], [1.0, 2.0], {0: Column([0, 255])})


def test_regular_geometry_checks_runs_without_scalar_array_counts():
    from array import array

    from tuner_under_test.engine.hybrid.physical_index import _regular_geometry

    class Values(array):
        def count(self, value):
            raise AssertionError("regular geometry scanned scalar array values")

        def __getitem__(self, item):
            result = super().__getitem__(item)
            return Values(self.typecode, result) if isinstance(item, slice) else result

    times, ends = _regular_geometry(Values("q", [1000000, 500, 500]), Values("q", [500] * 3))
    assert list(times) == [1000.0, 1000.5, 1001.0]
    assert list(ends) == [1000.5, 1001.0, 1001.5]


def test_regular_physical_decode_does_not_recheck_each_geometry_row(monkeypatch):
    from array import array

    from tuner_under_test.engine.hybrid import physical_index

    original = physical_index.PhysicalRows(
        (0,),
        array("d", [1000.0, 1000.5, 1001.0]),
        array("d", [1000.5, 1001.0, 1001.5]),
        {0: b"\1\2\3"},
    )
    encoded = original.encode()

    def redundant(*args):
        raise AssertionError("proved regular geometry compared rows again")

    monkeypatch.setattr(physical_index, "lt", redundant)
    decoded = physical_index.PhysicalRows.decode((0,), encoded)
    times, ends, columns = decoded.window(1000, 1002)
    assert list(times) == [1000.0, 1000.5, 1001.0]
    assert list(ends) == [1000.5, 1001.0, 1001.5]
    assert columns == {0: b"\1\2\3"}
    decoded.close()
    original.close()


@pytest.mark.parametrize("duration", [0.0, 0.75])
def test_regular_physical_decode_still_rejects_invalid_durations(duration):
    from array import array

    from tuner_under_test.engine.hybrid.physical_index import PhysicalRows

    starts = array("d", [1000.0, 1000.5, 1001.0])
    original = PhysicalRows(
        (0,), starts, array("d", [t + duration for t in starts]), {0: b"\1\2\3"}
    )
    decoded = PhysicalRows.decode((0,), original.encode())
    try:
        with pytest.raises(ValueError, match="geometry"):
            decoded.window(1000, 1002)
    finally:
        decoded.close()
        original.close()
