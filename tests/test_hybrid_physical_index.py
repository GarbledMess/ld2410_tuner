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
