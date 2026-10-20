"""The numerical boundary preserves threshold crossings without HA runtime objects."""

import importlib.util
from array import array
from pathlib import Path

import pytest
import test_tuner  # noqa: F401
from tuner_under_test.engine.columns import percentage, quantize


@pytest.mark.parametrize("value", [None, float("nan"), float("inf"), -0.01, 100.01])
def test_invalid_percentage_is_missing(value):
    assert percentage(value) is None


@pytest.mark.parametrize("value", [0, 0.01, 20, 20.2, 99.99, 100])
def test_whole_percentage_changes_energy_by_less_than_one_and_keeps_crossings(value):
    result = percentage(value)
    assert isinstance(result, int)
    assert 0 <= result - value < 1
    assert all((result > threshold) == (value > threshold) for threshold in range(101))


@pytest.mark.parametrize(
    ("column", "expected"),
    [
        (bytes([0, 1, 100, 255]), bytes([0, 1, 100, 255])),
        (array("f", [0, 0.01, 99.01, float("nan")]), bytes([0, 1, 100, 255])),
    ],
)
def test_quantized_column_preserves_every_integer_threshold_comparison(column, expected):
    before = bytes(column)
    result = quantize(column, 0, len(column))
    assert result == expected
    assert isinstance(result, bytes)
    assert bytes(column) == before
    for index, encoded in enumerate(result):
        if encoded != 255:
            assert all((encoded > t) == (column[index] > t) for t in range(101))


def test_quantization_clips_and_handles_empty_columns():
    assert quantize(bytes([1, 2, 3]), 1, 2) == b"\x02"
    assert quantize(array("f", [1.1, 2.1, 3.1]), 1, 2) == b"\x03"
    assert quantize(b"", 0, 0) == b""
    assert quantize(array("f"), 0, 0) == b""


@pytest.mark.parametrize(("first", "after"), [(-1, 1), (2, 1), (0, 4)])
def test_invalid_column_extent_is_rejected(first, after):
    with pytest.raises(ValueError, match="column extent"):
        quantize(bytes([1, 2, 3]), first, after)


def test_kernel_loads_without_importing_the_integration():
    source = (
        Path(__file__).resolve().parents[1] / "custom_components/ld2410_tuner/engine/columns.py"
    )
    spec = importlib.util.spec_from_file_location("standalone_columns", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.quantize(bytes([42]), 0, 1) == bytes([42])
