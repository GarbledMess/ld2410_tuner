"""Bounded, detached worker history acquisition."""

import asyncio
import datetime
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import test_tuner  # noqa: F401
from tuner_under_test.history import dayfile, daystore

DATE = datetime.date(2025, 10, 15)
START = daystore.bounds(DATE)[0]
END = START + 86400


def acquire(store, *args, **kwargs):
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(store.worker_snapshot, "radar", DATE, *args, **kwargs).result()


def live_store(tmp_path):
    store = daystore.DayStore(tmp_path)
    store.append("radar", dayfile.Change(START + 1, {0: 1.25, 1: None}))
    store.append("radar", dayfile.Guess(START + 1, "present", 2, 0))
    return store


def test_live_detached_matches_read_and_retains_legacy_values(tmp_path):
    store = live_store(tmp_path)
    expected = store.day("radar", DATE, until=START + 5)
    snapshot = acquire(store, until=START + 5)
    assert snapshot.file is None
    assert snapshot.cutoff == START + 5
    assert snapshot.day == expected
    assert not hasattr(snapshot.day, "_summary_owner")
    assert not hasattr(snapshot.day, "_summary_version")
    assert snapshot.day.vector(0)[:2] == (1.25, None)
    store.append("radar", dayfile.Change(START + 2, {0: 9}))
    assert list(snapshot.day.times) == [START + 1]
    assert list(snapshot.day.ends) == [START + 5]
    snapshot.day.columns[0][0] = 7
    assert store.day("radar", DATE, until=START + 5).vector(0)[0] == 1.25


def test_closing_snapshot_matches_read_and_preserves_separate_late(tmp_path):
    store = live_store(tmp_path)
    store.append("radar", dayfile.Change(END + 1, {0: 9}))
    span = dayfile.GuessSpan(END + 2, START + 8, START + 10, "not_present", 1, 0)
    store.append("radar", span)
    snapshot = acquire(store, until=START + 3)
    assert snapshot.cutoff == END
    assert snapshot.day == store._closing["radar"][DATE].builder.day()
    assert snapshot.late_guesses == (span,)
    store._late["radar"][DATE].clear()
    assert snapshot.late_guesses == (span,)


def test_finished_reference_avoids_decode_even_with_late(tmp_path, monkeypatch):
    store = live_store(tmp_path)
    store.flush(now=END + 1)
    span = dayfile.GuessSpan(END + 2, START + 8, START + 10, "present", 1, 0)
    store.append("radar", span)
    monkeypatch.setattr(dayfile, "decode_day", lambda *_: pytest.fail("decoded"))
    monkeypatch.setattr(Path, "read_bytes", lambda *_: pytest.fail("read"))
    snapshot = acquire(store)
    assert snapshot.day is None
    assert snapshot.file.path == store._path("radar", DATE, ".day")
    stat = snapshot.file.path.stat()
    assert snapshot.file.identity == (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size)
    assert snapshot.late_guesses == (span,)
    assert snapshot.revision == 1
    assert snapshot.generation == snapshot.rewrites == 0
    assert snapshot.changes == 1


def test_slow_finished_file_stat_does_not_block_recording(tmp_path, monkeypatch):
    store = live_store(tmp_path)
    store.flush(now=END + 1)
    target = store._path("radar", DATE, ".day")
    entered, release = threading.Event(), threading.Event()
    stat = Path.stat

    def blocked(path, **kwargs):
        if path == target:
            entered.set()
            assert release.wait(5), "test failed to release synthetic file I/O"
        return stat(path, **kwargs)

    monkeypatch.setattr(Path, "stat", blocked)
    with ThreadPoolExecutor(max_workers=2) as executor:
        snapshot = executor.submit(store.worker_snapshot, "radar", DATE)
        try:
            assert entered.wait(5)
            recording = executor.submit(store.append, "other", dayfile.Change(END + 2, {0: 7}))
            recording.result(timeout=1)
        finally:
            release.set()
        assert snapshot.result(timeout=5).file.path == target


def test_missing_reference_preserves_late_and_markers(tmp_path):
    store = daystore.DayStore(tmp_path)
    span = dayfile.GuessSpan(END + 2, START + 8, START + 10, "present", 1, 0)
    store.append("radar", span)
    store._generations["radar"] = 3
    store._rewrites["radar"] = 2
    snapshot = acquire(store)
    assert snapshot.file is snapshot.day is None
    assert snapshot.late_guesses == (span,)
    assert (snapshot.generation, snapshot.rewrites) == (3, 2)


@pytest.mark.parametrize(
    "field,value", [("max_rows", 0), ("max_guesses", 0), ("max_raw_bytes", 36)]
)
def test_limits_reject_before_view(tmp_path, monkeypatch, field, value):
    from tuner_under_test.history.worker_snapshot import Limits, SnapshotTooLarge

    store = live_store(tmp_path)
    monkeypatch.setattr(dayfile.DayBuilder, "view", lambda *_: pytest.fail("copied"))
    with pytest.raises(SnapshotTooLarge):
        acquire(store, until=START, limits=Limits(**{field: value}))


def test_limit_counts_full_guesses_confirmed_and_late_before_filter(tmp_path, monkeypatch):
    from tuner_under_test.history.worker_snapshot import Limits, SnapshotTooLarge

    store = live_store(tmp_path)
    builder = store._live["radar"].builder
    builder.confirmed.append((START + 99, (START + 10, START + 11, "present", 1, 0)))
    monkeypatch.setattr(dayfile.DayBuilder, "view", lambda *_: pytest.fail("copied"))
    with pytest.raises(SnapshotTooLarge):
        acquire(store, until=START, limits=Limits(max_guesses=1))


def test_exact_limits_and_cutoff_clipping(tmp_path, monkeypatch):
    from tuner_under_test.history.worker_snapshot import Limits

    store = live_store(tmp_path)
    limits = Limits(max_rows=1, max_guesses=1, max_raw_bytes=37)
    assert acquire(store, until=END + 1, limits=limits).day.ends[-1] == END
    assert not acquire(store, until=START - 1, limits=limits).day.times
    store.observe("radar", START + 100)
    monkeypatch.setattr("tuner_under_test.history.worker_snapshot.time.time", lambda: START + 5)
    assert acquire(store, limits=limits).cutoff == START + 100


@pytest.mark.parametrize("field", ["max_rows", "max_guesses", "max_raw_bytes"])
@pytest.mark.parametrize("value", [-1, 1.5, True])
def test_invalid_limits(field, value):
    from tuner_under_test.history.worker_snapshot import Limits

    with pytest.raises(ValueError):
        Limits(**{field: value})


def test_refuses_main_thread_and_event_loop_thread(tmp_path):
    store = live_store(tmp_path)
    with pytest.raises(RuntimeError, match="executor"):
        store.worker_snapshot("radar", DATE)

    async def in_loop():
        with pytest.raises(RuntimeError, match="executor"):
            store.worker_snapshot("radar", DATE)

    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(asyncio.run, in_loop()).result()


def test_stat_failure_is_not_silently_missing(tmp_path, monkeypatch):
    store = daystore.DayStore(tmp_path)
    original = Path.stat
    target = store._path("radar", DATE, ".day")

    def stat(path, **kwargs):
        if path == target:
            raise PermissionError("no")
        return original(path, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)
    with pytest.raises(PermissionError):
        acquire(store)


@pytest.mark.parametrize("kind", ["rows", "guesses", "confirmed"])
def test_default_limits_bound_full_builder_before_early_prefix(tmp_path, monkeypatch, kind):
    from tuner_under_test.history.worker_snapshot import Limits, SnapshotTooLarge

    store = live_store(tmp_path)
    builder = store._live["radar"].builder
    limits = Limits()
    if kind == "rows":
        builder.times.extend([START + 99] * limits.max_rows)
    elif kind == "guesses":
        builder.guesses.extend([(START + 99, END, "present", 1, 0)] * limits.max_guesses)
    else:
        builder.confirmed.extend(
            [(START + 99, (START + 99, END, "present", 1, 0))] * limits.max_guesses
        )
    monkeypatch.setattr(dayfile.DayBuilder, "view", lambda *_: pytest.fail("copied"))
    with pytest.raises(SnapshotTooLarge):
        acquire(store, until=START)


def test_finished_late_limit_rejected_before_span_copy_or_stat(tmp_path, monkeypatch):
    from tuner_under_test.history.worker_snapshot import Limits, SnapshotTooLarge

    class NoCopy(list):
        def __iter__(self):
            pytest.fail("copied spans")

    store = daystore.DayStore(tmp_path)
    store._late["radar"] = {DATE: NoCopy([object()])}
    with pytest.raises(SnapshotTooLarge):
        acquire(store, limits=Limits(max_guesses=0))


def test_zero_budgets_accept_empty_missing_day(tmp_path):
    from tuner_under_test.history.worker_snapshot import Limits

    snapshot = acquire(daystore.DayStore(tmp_path), limits=Limits(0, 0, 0))
    assert snapshot.day is snapshot.file is None
    assert snapshot.late_guesses == ()


def test_closing_includes_confirmed_spans(tmp_path):
    store = daystore.DayStore(tmp_path)
    store.append("radar", dayfile.Change(START + 1, {0: 3}))
    store.append("radar", dayfile.GuessSpan(START + 10, START + 2, START + 8, "present", 1, 0))
    store.append("radar", dayfile.End(END + 1))
    snapshot = acquire(store)
    assert snapshot.day.guesses == [(START + 2, START + 8, "present", 1, 0)]
    assert snapshot.day == store.day("radar", DATE)


def test_reference_can_disappear_after_capture(tmp_path):
    store = live_store(tmp_path)
    store.flush(now=END + 1)
    snapshot = acquire(store)
    snapshot.file.path.unlink()
    assert snapshot.file.identity[-1] > 0
    assert acquire(store).file is None
