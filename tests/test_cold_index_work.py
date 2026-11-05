"""Synthetic bucket equivalence without per-cell array allocation."""

import random
import struct
import sys
from array import array
from io import BytesIO

import pytest
import test_tuner  # noqa: F401
from tuner_under_test.engine.hybrid import index


def test_energy_bucket_allocations_are_bounded_by_energies(monkeypatch):
    allocations = []

    def counted(*args):
        allocations.append(args[0])
        return array(*args)

    monkeypatch.setattr(index, "array", counted)
    column = bytes(range(101)) * 100 + b"\xff" * 10
    actual = index._column(column)
    assert actual == {energy: array("H", range(energy, 10100, 101)) for energy in range(101)}
    assert len(allocations) <= 101


def test_read_positions_validates_without_per_position_python_generators():
    positions = array("H", range(14000))
    calls = []

    def profile(frame, event, _arg):
        if event == "call" and frame.f_code.co_filename == index.__file__:
            calls.append(frame.f_code.co_name)

    encoded = b"\x01" + struct.pack("<BH", 20, len(positions))
    encoded += struct.pack("<" + "H" * len(positions), *positions)
    sys.setprofile(profile)
    try:
        actual = index._read_column(BytesIO(encoded), 14400)
    finally:
        sys.setprofile(None)
    assert actual == {20: positions}
    assert calls.count("<genexpr>") < 10


def test_bucket_arrays_are_created_in_bulk(monkeypatch):
    class Positions(array):
        appends = 0

        def append(self, value):
            type(self).appends += 1
            super().append(value)

    monkeypatch.setattr(index, "array", Positions)
    column = bytes(range(101)) * 100
    assert index._column(column) == {
        energy: array("H", range(energy, 10100, 101)) for energy in range(101)
    }
    assert Positions.appends == 0


@pytest.mark.parametrize("count", [0, 1, 127, 128, 14400, 16384])
def test_few_energy_columns_use_bulk_position_compaction(count):
    column = bytes((0, 20, 100, 255, 254)[i % 5] for i in range(count))
    appends = [0]

    def profile(_frame, event, argument):
        if event == "c_call" and getattr(argument, "__name__", "") == "append":
            appends[0] += 1

    sys.setprofile(profile)
    try:
        actual = index._column(column)
    finally:
        sys.setprofile(None)
    assert actual == {
        energy: array("H", range(offset, count, 5))
        for offset, energy in enumerate((0, 20, 100))
        if offset < count
    }
    assert not appends[0]


@pytest.mark.parametrize("positions", [(0, 0), (2, 1), (0, 14400)])
def test_read_positions_rejects_invalid_buckets(positions):
    encoded = b"\x01" + struct.pack("<BHHH", 20, 2, *positions)
    with pytest.raises(ValueError):
        index._read_column(BytesIO(encoded), 14400)


def test_index_writer_preserves_wire_bytes_without_boxing_positions():
    iterations = []

    class Positions(array):
        def __iter__(self):
            iterations.append(1)
            return super().__iter__()

    positions = Positions("H", [0, 127, 128, 14399, 65535])
    value = index.EnergyIndex(({20: positions},) * 18, ((20, 20),) * 18, 65536)
    stream = BytesIO()
    index.write_index(stream, value)
    expected = b"\x12" + (b"\x01" + struct.pack("<BH5H", 20, 5, 0, 127, 128, 14399, 65535)) * 18
    assert stream.getvalue() == expected
    assert not iterations


@pytest.mark.parametrize(
    "energies,count", [(1, 14400), (8, 16384), (9, 14400), (8, 16385), (101, 14400)]
)
def test_bucket_paths_match_seeded_scalar_positions(energies, count):
    rng = random.Random(9240 + energies)
    alphabet = [*range(energies), 101, 254, 255]
    column = bytes(rng.choice(alphabet) for _ in range(count))
    expected = {}
    for position, energy in enumerate(column):
        if energy <= 100:
            expected.setdefault(energy, []).append(position)
    expected = {energy: array("H", values) for energy, values in expected.items()}
    actual = index._column(column)
    assert actual == expected
    value = index.EnergyIndex((actual,) * 18, (), count)
    stream = BytesIO()
    index.write_index(stream, value)
    assert stream.getvalue() == b"\x12" + b"".join(
        bytes([len(expected)])
        + b"".join(
            struct.pack("<BH", energy, len(values)) + struct.pack("<" + "H" * len(values), *values)
            for energy, values in sorted(expected.items())
        )
        for _ in range(18)
    )
    stream.seek(0)
    assert index.read_index(stream, count).buckets == (expected,) * 18


@pytest.mark.parametrize("energies", [9, 10, 12])
def test_low_cardinality_day_columns_avoid_scalar_position_appends(energies):
    column = bytes(range(energies)) * (14400 // energies)
    appends = []

    def profile(_frame, event, argument):
        if event == "c_call" and getattr(argument, "__name__", "") == "append":
            appends.append(1)

    sys.setprofile(profile)
    try:
        actual = index._column(column)
    finally:
        sys.setprofile(None)
    assert actual == {
        energy: array("H", range(energy, len(column), energies)) for energy in range(energies)
    }
    assert len(appends) == 0


def test_sorted_bucket_bounds_do_not_rescan_positions(monkeypatch):
    iterations = []

    class Positions(array):
        def __iter__(self):
            iterations.append(1)
            return super().__iter__()

    monkeypatch.setattr(index, "array", Positions)
    encoded = b"\x01" + struct.pack("<BH4H", 20, 4, 0, 1, 500, 14399)
    actual = index._read_column(BytesIO(encoded), 14400)
    assert actual == {20: array("H", [0, 1, 500, 14399])}
    assert len(iterations) == 1


@pytest.mark.parametrize("positions", [(14400, 1), (65535, 2), (0, 14400)])
def test_out_of_bounds_positions_still_rejected_when_not_last(positions):
    encoded = b"\x01" + struct.pack("<BH2H", 20, 2, *positions)
    with pytest.raises(ValueError):
        index._read_column(BytesIO(encoded), 14400)


def test_empty_positions_keep_negative_bound_rejection():
    encoded = b"\x01" + struct.pack("<BH", 20, 0)
    with pytest.raises(ValueError, match="Invalid energy positions"):
        index._read_column(BytesIO(encoded), -1)
