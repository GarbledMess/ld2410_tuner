"""Results derived from a finished day are kept until that day or its labels change
(user, 2026-10-16: a day "only needs to be recalculated if the human set presence status has
been changed"). Live days are never kept."""

import datetime
from unittest.mock import patch

import pytest
import test_tuner  # noqa: F401
from tuner_under_test.history import day_results, dayfile, daystore
from tuner_under_test.history.label_lookup import _history_label_reader

START = 1_760_486_400.0  # 2025-10-15 00:00 UTC.
DATE = datetime.date(2025, 10, 15)


def finished_store(tmp_path):
    store = daystore.DayStore(tmp_path / "history")
    store.open(now=START)
    for i in range(10):
        store.append("radar", dayfile.Change(START + i * 60, {0: i}))
    store.append("radar", dayfile.Change(START + 86_400 + 1, {0: 1}))  # Next day: live.
    store.flush(now=START + 86_400 + 2)
    return store


def counted(store, args=()):
    calls = []

    def compute():
        calls.append(1)
        return store.day("radar", DATE).times[-1]

    return day_results.remembered(store, "radar", DATE, "last", args, compute), calls


@pytest.fixture(autouse=True)
def empty_cache():
    day_results.clear()


def test_a_finished_day_result_is_computed_once(tmp_path):
    store = finished_store(tmp_path)
    first, calls = counted(store)
    again, more = counted(store)
    assert first == again == START + 540
    assert (len(calls), len(more)) == (1, 0)
    _other, different = counted(store, args=("other",))
    assert len(different) == 1


def test_rewriting_the_day_recomputes(tmp_path):
    store = finished_store(tmp_path)
    counted(store)
    day = store.day("radar", DATE)
    store.replace("radar", DATE, dayfile.splice(day, []))  # Same content, a new write.
    _value, calls = counted(store)
    assert len(calls) == 1


def test_live_days_and_days_with_late_guesses_are_never_kept(tmp_path):
    store = finished_store(tmp_path)
    live = DATE + datetime.timedelta(days=1)
    for _ in range(2):
        found = day_results.remembered(store, "radar", live, "x", (), lambda: object())
    assert day_results.remembered(store, "radar", live, "x", (), lambda: 1) == 1
    assert found is not None
    store.append(
        "radar", dayfile.GuessSpan(START + 86_400 + 3, START + 10, START + 20, "present", 18, 2)
    )
    calls = []
    for _ in range(2):
        day_results.remembered(store, "radar", DATE, "y", (), lambda: calls.append(1))
    assert len(calls) == 2


def test_label_fingerprint_changes_only_with_labels_inside_the_day():
    labels = [
        {"start": START + 100, "end": START + 200, "state": "present"},
        {"start": START + 90_000, "end": START + 90_100, "state": "not_present"},
    ]
    read = _history_label_reader({"history_labels": labels})
    before = day_results.fingerprint(read, START, START + 86_400)
    labels[1]["state"] = "present"  # Another day.
    assert (
        day_results.fingerprint(
            _history_label_reader({"history_labels": labels}), START, START + 86_400
        )
        == before
    )
    labels[0]["state"] = "not_present"  # This day.
    assert (
        day_results.fingerprint(
            _history_label_reader({"history_labels": labels}), START, START + 86_400
        )
        != before
    )


def test_kept_results_need_no_decoding(tmp_path):
    store = finished_store(tmp_path)
    counted(store)
    store._cache.clear()
    with patch.object(dayfile, "decode_day", side_effect=AssertionError("decoded again")):
        value, calls = counted(store)
    assert value == START + 540 and not calls


def test_guess_runs_of_finished_days_are_read_once(tmp_path):
    from types import SimpleNamespace

    from tuner_under_test.history import guesses

    store = daystore.DayStore(tmp_path / "history")
    store.open(now=START)
    store.append("radar", dayfile.Change(START, {0: 5}))
    store.append("radar", dayfile.Guess(START + 10, "present", 18, guesses.REFERENCE))
    store.append("radar", dayfile.Change(START + 86_400 + 1, {0: 1}))
    store.flush(now=START + 86_400 + 2)
    view = SimpleNamespace(_days=store, _history_until=START + 86_400 + 5)
    first = guesses.recorded(view, "radar")
    store._cache.clear()
    with patch.object(dayfile, "decode_day", side_effect=AssertionError("decoded again")):
        assert guesses.recorded(view, "radar") == first
    assert first[0][:3] == (START + 10, START + 86_400 + 5, "present")


def dense_store(tmp_path, days=3):
    store = daystore.DayStore(tmp_path / "history")
    store.open(now=START)
    for i in range(days * 1440):
        store.append("radar", dayfile.Change(START + i * 60, {0: i % 70, 1: (i * 7) % 90}))
    store.flush(now=START + days * 86_400 - 30)
    return store


def test_hybrid_digests_reuse_what_they_derived_from_finished_days(tmp_path):
    from types import SimpleNamespace

    from tuner_under_test.history.fit_days import FittingDays
    from tuner_under_test.worker import history
    from tuner_under_test.worker.calculations import ReadOnlyDays

    store = dense_store(tmp_path)
    dates = store.days("radar")
    entries = []
    for date in dates[:-1]:
        path = store._path("radar", date, ".day")
        entries.append(
            {
                "device_id": "radar",
                "date": date.isoformat(),
                "path": str(path),
                "identity": history._identity(path.stat()),
                "late": [],
                "persistent": True,
            }
        )
    days = ReadOnlyDays({"days": entries, "budgets": {"max_file_bytes": None}})
    view = SimpleNamespace(_days=days, _history_until=START + 3 * 86_400 - 30)
    fitting = FittingDays(view)
    first = [fitting.digest("radar", date, (0, 1)) for date in dates[:-1]]
    assert len(first) == 2
    assert all(list(digest.segments()) for digest in first)
    # A new wrapper emulates a warm job; finished physical days need no row decoding.
    with patch.object(
        days, "open_columns", side_effect=AssertionError("finished day decoded again")
    ):
        again = [FittingDays(view).digest("radar", date, (0, 1)) for date in dates[:-1]]
    for before, after in zip(first, again, strict=True):
        assert before.channels == after.channels
        assert before.channels[0] != before.channels[1]
        assert list(before.segments()) == list(after.segments())


def test_quality_evidence_reuses_what_it_derived_from_finished_days(tmp_path):
    from types import SimpleNamespace

    from tuner_under_test.calibration import summary_quality

    store = dense_store(tmp_path)
    labels = [
        {"start": START + t, "end": START + t + 3600, "state": ("present", "not_present")[k % 2]}
        for k, t in enumerate(range(0, 3 * 86_400, 3600))
    ]
    device = {"history_labels": labels}
    read = _history_label_reader(device)
    end = START + 3 * 86_400 - 30
    view = SimpleNamespace(data={"devices": {"radar": device}}, _days=store, _history_until=end)
    keys = ["g0_move", "g1_move"]
    first = summary_quality.collect(view, "radar", keys, START, end, read, None)
    assert first.episodes
    store._cache.clear()
    finished = set(store.days("radar")[:-1])
    real = store.day

    def day(device_id, date, until=None):
        assert date not in finished, "finished day decoded again"
        return real(device_id, date, until)

    with patch.object(store, "day", side_effect=day):
        again = summary_quality.collect(view, "radar", keys, START, end, read, None)
    assert again == first


def test_kept_results_are_bounded_by_size(tmp_path, monkeypatch):
    from array import array

    store = finished_store(tmp_path)
    monkeypatch.setattr(day_results, "MAX_BYTES", 100_000)
    for i in range(10):
        day_results.remembered(store, "radar", DATE, "big", (i,), lambda: array("d", [0.0]) * 4000)
    assert day_results.kept_bytes() <= 100_000
    calls = []
    day_results.remembered(store, "radar", DATE, "big", (9,), lambda: calls.append(1))
    assert not calls  # The newest stays.


def test_oversized_result_is_returned_without_evicting_useful_cache(tmp_path, monkeypatch):
    store = finished_store(tmp_path)
    monkeypatch.setattr(day_results, "MAX_BYTES", 1024)
    small = day_results.remembered(store, "radar", DATE, "small", (), lambda: b"useful")
    before = day_results.kept_bytes()
    calls = []

    def oversized():
        calls.append(1)
        return bytes(2048)

    for _ in range(2):
        assert len(day_results.remembered(store, "radar", DATE, "huge", (), oversized)) == 2048
        assert day_results.kept_bytes() == before
    assert calls == [1, 1]
    assert day_results.remembered(store, "radar", DATE, "small", (), lambda: None) == small
