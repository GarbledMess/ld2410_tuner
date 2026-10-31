"""Synthetic captured guesses do not decode or retain physical radar days."""

from array import array
from datetime import date

import pytest
import test_tuner  # noqa: F401
from test_hybrid_rows import reference, source
from tuner_under_test.history import dayfile
from tuner_under_test.worker import history


def encoded():
    return dayfile.encode_day(
        dayfile.Day(
            0,
            86400,
            array("d", [0]),
            array("d", [100]),
            [bytes([20])] * 18,
            [(0, 10, "present", 12, 1), (20, 30, "not_present", 15, 2)],
        )
    )


def test_captured_guess_reader_never_builds_physical_index(tmp_path, monkeypatch):
    _, days, path = source(tmp_path, encoded())
    monkeypatch.setattr(history._ColumnReader, "_index_times", lambda *a: pytest.fail("row index"))
    expected = dayfile.decode_guesses(encoded())
    assert list(history.guess_runs(reference(path))) == expected
    assert days.guess_runs("synthetic", date(1970, 1, 1)) == expected
    assert not days.shared.cache


def test_captured_guesses_hide_future_and_late_confirmations(tmp_path):
    _, days, _ = source(tmp_path, encoded())
    days.entries[("synthetic", date(1970, 1, 1))]["late"] = [
        [50, 10, 20, "present", 16, 2],
        [12, 5, 20, "not_present", 14, 2],
    ]
    assert days.guess_runs("synthetic", date(1970, 1, 1), 15) == [
        (0, 10, "present", 12, 1),
        (10, 15, "not_present", 14, 2),
    ]


def test_captured_guess_reader_checks_identity_after_consumption(tmp_path):
    _, _, path = source(tmp_path, encoded())
    iterator = history.guess_runs(reference(path))
    next(iterator)
    path.write_bytes(encoded() + b"changed")
    with pytest.raises(history.HistoryRejected, match="identity changed"):
        list(iterator)


def test_captured_guesses_with_no_file_keep_visible_pending_spans(tmp_path):
    _, days, _ = source(tmp_path)
    days.entries[("synthetic", date(1970, 1, 1))].update(
        path=None,
        late=[[5, 0, 4, "present", 15, 2]],
    )
    assert days.guess_runs("synthetic", date(1970, 1, 1), 6) == [(0, 4, "present", 15, 2)]
    assert days.guess_runs("absent", date(1970, 1, 1), 6) == []


@pytest.mark.parametrize("cutoff", [None, 100000])
def test_finished_day_sees_confirmations_recorded_on_later_days(tmp_path, cutoff):
    _, days, _ = source(tmp_path, encoded())
    days.entries[("synthetic", date(1970, 1, 1))]["late"] = [
        [90000, 10, 20, "present", 16, 2],
    ]
    assert (10, 20, "present", 16, 2) in days.guess_runs("synthetic", date(1970, 1, 1), cutoff)
