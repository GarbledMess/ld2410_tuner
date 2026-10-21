"""Startup handoff budgets also protect flush and maintenance adapters."""

from array import array
from datetime import date
from unittest.mock import patch

import pytest
import test_tuner  # noqa: F401
from tuner_under_test.history import dayfile, daystore
from tuner_under_test.worker import flushing, maintenance, startup

DATE = date(2026, 10, 20)
START = daystore.bounds(DATE)[0]


def _dense_live():
    live = daystore._Live(DATE)
    builder = live.builder
    builder.times = array("d", (START + index / 2 for index in range(172800)))
    builder.ends = array("d", (time + 0.5 for time in builder.times))
    builder.columns = [bytearray([gate] * 172800) for gate in range(18)]
    builder.guesses = [(START + i, START + i + 1, "present", i % 101, 1) for i in range(86400)]
    return live


def test_dense_closing_capture_and_maintenance_roundtrip(tmp_path):
    live = _dense_live()
    store = daystore.DayStore(tmp_path / "history")
    store._closing = {"radar": {DATE: live}}
    capture = tmp_path / "capture"
    capture.mkdir()
    metadata, _ = flushing.capture(store, capture, START + 86401)
    recovered = flushing._load(
        {"root": str(tmp_path / "restored"), "directory": str(capture), "flush": metadata}
    )
    actual = recovered._closing["radar"][DATE].builder
    assert actual.times == live.builder.times
    assert actual.columns == live.builder.columns
    assert actual.guesses == live.builder.guesses
    day = dayfile.Day(
        START, START + 86400, actual.times, actual.ends, actual.columns, actual.guesses
    )
    descriptor = startup.write_day(day, tmp_path, 0)
    result = {
        "changes": [
            {
                "key": "radar",
                "date": DATE.isoformat(),
                "temporary": "candidate.trim",
                "day": descriptor,
            }
        ],
        "prepared": {},
        "report": {},
    }
    _, changes, _, _ = maintenance.load(result, tmp_path)
    assert changes[0][2].times == actual.times
    assert changes[0][2].guesses == actual.guesses


def test_flushing_decoded_budget_batches_days_and_rejects_one_oversized_day_before_copy(tmp_path):
    # An empty closing day restores to 4,096 decoded bytes.
    store = daystore.DayStore(tmp_path)
    store._closing = {"a": {DATE: daystore._Live(DATE)}, "b": {DATE: daystore._Live(DATE)}}
    with patch.object(startup, "MAX_DECODED_BYTES", 6000):
        detached, _, ownership = flushing._detached(store, START + 86401)
    assert [key for key, _, _ in detached] == ["a"]
    assert [key for key, _, _ in ownership["items"]] == ["a"]
    assert set(store._closing) == {"a", "b"}  # Both remain owned until publication.
    with (
        patch.object(startup, "MAX_DECODED_BYTES", 4000),
        patch.object(flushing.copy, "deepcopy", side_effect=AssertionError("copied")),
    ):
        with pytest.raises(ValueError, match="decoded"):
            flushing._detached(store, START + 86401)


def test_maintenance_aggregate_budget_rejected_before_any_read(tmp_path):
    descriptor = {"rows": 0, "floats": [False] * 18}
    change = {"temporary": "day.trim", "day": descriptor}
    with (
        patch.object(startup, "MAX_DECODED_BYTES", 6000),
        patch.object(startup, "read_day", side_effect=AssertionError("read")),
    ):
        with pytest.raises(ValueError, match="decoded"):
            maintenance.load({"changes": [change, change]}, tmp_path)


def test_flush_message_limit_independent_of_raw_handoff(tmp_path):
    store = daystore.DayStore(tmp_path)
    with patch.object(flushing, "MAX_MESSAGE_BYTES", 1):
        with pytest.raises(ValueError):
            flushing.capture(store, tmp_path, START)


def test_maintenance_preserves_more_than_128_small_replacements(tmp_path):
    descriptor = {"rows": 0, "floats": [False] * 18}
    changes = [
        {
            "key": str(index),
            "date": DATE.isoformat(),
            "temporary": f"day-{index}.trim",
            "day": descriptor,
        }
        for index in range(129)
    ]
    with patch.object(startup, "read_day", return_value=None):
        result = maintenance.load({"changes": changes, "prepared": {}, "report": {}}, tmp_path)
    assert len(result[1]) == 129
