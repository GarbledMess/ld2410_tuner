"""Cold worker counts preserve the parent's incremental accounting."""

from array import array
from datetime import date
from types import SimpleNamespace

import pytest
import test_tuner  # noqa: F401
from tuner_under_test.history import dayfile, label_counts, summaries


def day(start, end, value):
    return dayfile.Day(
        start,
        end,
        array("d", [start]),
        array("d", [end]),
        [bytes([value])] + [bytes([255])] * 17,
        [],
    )


def test_recount_reconstructs_identity_and_replaces_live_without_double_counting():
    from tuner_under_test.worker import recount

    finished, live = day(10, 20, 7), day(30, 35, 9)
    days = {date(1970, 1, 1): finished, date(1970, 1, 2): live}
    store = SimpleNamespace(summary=lambda device_id, date, **kw: summaries.of_day(days[date]))
    view = SimpleNamespace(_days=store, _history_until=35)
    index = ([0], ["present"])
    result = recount.calculate(view, "a", 0, index, ["1970-01-01"], "1970-01-02")
    block = {"date": date(1970, 1, 1), "start": 10, "end": 20, "days": store, "device_id": "a"}
    counts = label_counts._from_worker([block], 0, index, result)
    assert counts.regular[id(block)][0] is block
    assert counts.seconds["g0_move"]["present"][7] == 10
    assert counts.seconds["g0_move"]["present"][9] == 5
    extended = day(30, 38, 9)
    assert counts.update({id(block): block}, extended, 0, index)
    assert counts.seconds["g0_move"]["present"][7] == 10
    assert counts.seconds["g0_move"]["present"][9] == 8
    assert counts.live is extended


def test_recount_empty_history_and_cutoff():
    from tuner_under_test.worker import recount

    store = SimpleNamespace(summary=lambda *args, **kw: summaries.of_day(day(10, 20, 7)))
    view = SimpleNamespace(_days=store)
    result = recount.calculate(view, "a", 15, ([0], ["not_present"]), ["1970-01-01"], None)
    assert result["seconds"]["g0_move"]["not_present"][7] == 5
    assert result["live"] is None
    empty = recount.calculate(view, "a", 0, ([], []), [], None)
    counts = label_counts._from_worker([], 0, ([], []), empty)
    assert sum(counts.seconds["g0_move"]["present"]) == 0


def test_recount_bad_finished_day_remains_inert():
    from tuner_under_test.worker import recount

    def unreadable(*args, **kwargs):
        raise ValueError("broken day")

    view = SimpleNamespace(_days=SimpleNamespace(summary=unreadable))
    result = recount.calculate(view, "a", 0, ([], []), ["1970-01-01"], None)
    block = {"date": date(1970, 1, 1), "start": 0, "end": 100}
    counts = label_counts._from_worker([block], 0, ([], []), result)
    assert counts.inert[id(block)] is block
    with pytest.raises(ValueError, match="broken day"):
        recount.calculate(view, "a", 0, ([], []), [], "1970-01-01")


def test_captured_live_histogram_subtracted_when_label_changes():
    from tuner_under_test.worker import recount

    live = day(30, 35, 9)
    view = SimpleNamespace(
        _days=SimpleNamespace(summary=lambda *args, **kw: summaries.of_day(live))
    )
    result = recount.calculate(view, "a", 0, ([0], ["present"]), [], "1970-01-01")
    counts = label_counts._from_worker([], 0, ([0], ["present"]), result)
    assert counts.update({}, live, 32, ([0], ["not_present"]))
    assert counts.seconds["g0_move"]["present"][9] == 0
    assert counts.seconds["g0_move"]["not_present"][9] == 3


def test_recount_propagates_worker_history_budget_rejection():
    from tuner_under_test.worker import history, recount

    def oversized(*args, **kwargs):
        raise history.HistoryRejected("budget")

    view = SimpleNamespace(_days=SimpleNamespace(summary=oversized))
    with pytest.raises(history.HistoryRejected, match="budget"):
        recount.calculate(view, "a", 0, ([], []), ["1970-01-01"], None)


def test_month_of_history_fits_the_result_protocol_without_repeating_histograms():
    from datetime import timedelta

    from tuner_under_test.worker import codec, recount

    view = SimpleNamespace(
        _days=SimpleNamespace(summary=lambda *args, **kw: summaries.of_day(day(10, 20, 7)))
    )
    dates = [(date(1970, 1, 1) + timedelta(days=index)).isoformat() for index in range(30)]
    result = recount.calculate(view, "a", 0, ([0], ["present"]), dates, dates[-1])
    restored = codec.unpack_result(codec.pack_result(result))
    assert restored["seconds"]["g0_move"]["present"][7] == 300
    assert restored["live"]["g0_move"]["present"][7] == 10
