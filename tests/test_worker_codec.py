"""Closed result values survive a strict JSON boundary."""

import math

import pytest
import test_tuner  # noqa: F401
from tuner_under_test.worker import codec


def test_result_roundtrip_preserves_numeric_keys_and_tuples():
    value = {"histogram": {1: 2.5, 2.5: ("yes", None)}, "nested": [{"$tuple": []}]}
    assert codec.unpack_result(codec.pack_result(value)) == value


@pytest.mark.parametrize("value", [math.inf, math.nan, object(), {None: 1}])
def test_result_rejects_nonportable_values(value):
    with pytest.raises(ValueError):
        codec.pack_result({"value": value})


def test_source_aggregate_string_budget_is_checked_before_copy():
    with pytest.raises(ValueError, match="string budget"):
        codec.encode(["x" * 600000, "y" * 600000])


def test_source_limits_and_reserved_tags():
    with pytest.raises(ValueError, match="node"):
        codec.encode([0] * 32768)
    nested = []
    for _ in range(22):
        nested = [nested]
    with pytest.raises(ValueError, match="depth"):
        codec.encode(nested)
    assert codec.unpack_result(codec.pack_result({"$worker": "human value"})) == {
        "$worker": "human value"
    }


@pytest.mark.parametrize(
    "encoded",
    [
        {"$worker": "tuple", "items": {}, "extra": 1},
        {"$worker": "mapping", "items": [[None, 0]]},
        {"$worker": "mapping", "items": [[1, "a"], [1.0, "b"]]},
        {"$worker": "mapping", "items": ["bad"]},
    ],
)
def test_invalid_tagged_values(encoded):
    with pytest.raises(ValueError):
        codec.decode(encoded)


def test_result_caps_encoding_and_rejects_unknown_tags():
    from tuner_under_test.worker import protocol

    with pytest.raises(ValueError):
        codec.pack_result({"value": "x" * 1000}, max_bytes=100)
    with pytest.raises(ValueError):
        codec.unpack_result(
            protocol.pack("result", {"value": {"$worker": "executable", "items": []}})
        )
