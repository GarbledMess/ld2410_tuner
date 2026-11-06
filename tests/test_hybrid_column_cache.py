"""Finished physical columns are reused independently of labels and fit cutoffs."""

from array import array

import pytest
import test_tuner  # noqa: F401
from test_hybrid_facade import worker_view
from tuner_under_test.history.fit_rows import CapturedRowSource


def encoded(blocks):
    return [(b.times.tolist(), b.ends.tolist(), b.columns) for b in blocks]


def test_finished_columns_survive_new_jobs_labels_and_cutoffs(tmp_path, monkeypatch):
    values = [(i * 37) % 101 for i in range(2000)]
    view = worker_view(tmp_path, "r", values)
    next(iter(view._days.entries.values()))["persistent"] = True
    source = CapturedRowSource(view, "r", (0,))
    expected = encoded(source.blocks(0.125, 11999.75))
    calls, original = [], source._columns

    def counted(date):
        calls.append(date)
        return original(date)

    monkeypatch.setattr(CapturedRowSource, "_columns", lambda self, date: counted(date))
    view._days.shared = type(view._days.shared)()
    view._history_until = 24000
    view.data["devices"]["r"]["history_labels"] = [
        {"start": 3.25, "end": 6000.5, "state": "present"}
    ]
    assert encoded(CapturedRowSource(view, "r", (0,)).blocks(0.125, 11999.75)) == expected
    assert calls == []


def test_column_cache_is_channel_independent(tmp_path, monkeypatch):
    view = worker_view(tmp_path, "r", [10, 255, 30, 40])
    next(iter(view._days.entries.values()))["persistent"] = True
    list(CapturedRowSource(view, "r", (0,)).blocks(0, 24))

    def forbidden(*args):
        raise AssertionError("finished columns were reopened")

    monkeypatch.setattr(CapturedRowSource, "_columns", forbidden)
    blocks = list(CapturedRowSource(view, "r", (0, 1)).blocks(0, 24))
    assert blocks[0].columns[0] == bytes([10, 255, 30, 40])
    assert blocks[0].columns[1] == bytes([255]) * 4
    assert blocks[0].times == array("d", [0, 6, 12, 18])


@pytest.mark.parametrize("persistent", [False, True])
def test_column_cache_clipping_missing_and_empty(tmp_path, persistent):
    view = worker_view(tmp_path, "r", [10, 255, 30, 40])
    next(iter(view._days.entries.values()))["persistent"] = persistent
    source = CapturedRowSource(view, "r", (0,))
    first = encoded(source.blocks(3.125, 20.5))
    assert first == [
        (
            [3.125, 6.0, 12.0, 18.0],
            [6.0, 12.0, 18.0, 20.5],
            (bytes([10, 255, 30, 40]),) + (bytes([255]) * 4,) * 17,
        )
    ]
    assert encoded(source.blocks(3.125, 20.5)) == first
    assert list(source.blocks(24, 24)) == []
