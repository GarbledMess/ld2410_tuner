"""Off-loop publication preserves recording progress and clear ownership."""

import asyncio
import threading
from functools import wraps
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_tuner  # noqa: F401
from tuner_under_test.history import dayfile, daystore

START = 1760486400.0
DATE = daystore.date_of(START)


def async_case(fn):
    @wraps(fn)
    def run(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return run


def prepared(tmp_path):
    store = daystore.DayStore(tmp_path / "history")
    builder = dayfile.DayBuilder(*daystore.bounds(DATE))
    builder.add(dayfile.Change(START + 1, {0: 10}))
    builder.add(dayfile.End(START + 10))
    original = builder.day()
    store._write_day("a", DATE, original)
    builder = dayfile.DayBuilder(*daystore.bounds(DATE))
    builder.add(dayfile.Change(START + 3, {0: 20}))
    builder.add(dayfile.End(START + 5))
    replacement = builder.day()
    staged = store.stage([("a", DATE, replacement)])
    hass = SimpleNamespace(async_add_executor_job=lambda fn, *args: asyncio.to_thread(fn, *args))
    return store, staged, SimpleNamespace(_days=store, hass=hass), replacement


def test_reservation_exposes_replacement_before_disk_io(tmp_path):
    store, staged, _runtime, replacement = prepared(tmp_path)
    before = store.rewrites("a")
    token = store.reserve(staged)
    assert store.day("a", DATE) is replacement
    assert store.rewrites("a") == before + 1
    assert store.identity("a", DATE) is None
    store.publish_reserved(token)
    assert dayfile.decode_day(store._path("a", DATE, ".day").read_bytes()).vector(0)[0] == 20


@async_case
async def test_commit_accepts_before_wait_and_recording_keeps_progressing(tmp_path, monkeypatch):
    from tuner_under_test.history import commit

    store, staged, runtime, replacement = prepared(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = Path.replace

    def held(path, target):
        if path == staged[0][3]:
            entered.set()
            assert release.wait(5)
        return original(path, target)

    monkeypatch.setattr(Path, "replace", held)
    accepted = []
    work = asyncio.create_task(
        commit.publish(runtime, staged, lambda: True, lambda: accepted.append(1))
    )
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        assert accepted == [1]
        assert store.day("a", DATE) is replacement
        store.append("a", dayfile.Change(START + 86401, {0: 30}))
        assert store.current("a")[0] == 30
        assert not work.done()
    finally:
        release.set()
    assert await work is True


@async_case
async def test_rejected_validation_never_publishes(tmp_path):
    from tuner_under_test.history import commit

    store, staged, runtime, _replacement = prepared(tmp_path)
    accepted = []
    assert await commit.publish(runtime, staged, lambda: False, lambda: accepted.append(1)) is False
    assert accepted == []
    assert store.day("a", DATE).vector(0)[0] == 10
    assert not staged[0][3].exists()


@async_case
async def test_clear_while_publication_waits_does_not_restore_history(tmp_path):
    from tuner_under_test.history import commit

    store, staged, runtime, _replacement = prepared(tmp_path)
    entered, release = threading.Event(), threading.Event()

    def hold_lock():
        with store._fs_lock:
            entered.set()
            assert release.wait(5)

    blocker = asyncio.create_task(asyncio.to_thread(hold_lock))
    assert await asyncio.to_thread(entered.wait, 5)
    accepted = asyncio.Event()
    work = asyncio.create_task(commit.publish(runtime, staged, lambda: True, accepted.set))
    await accepted.wait()
    cleanup = commit.clear_device(runtime, "a")
    assert store.day("a", DATE) is None
    store.append("a", dayfile.Change(START + 86401, {0: 40}))
    release.set()
    await blocker
    await work
    await cleanup
    assert not store._path("a", DATE, ".day").exists()
    await asyncio.to_thread(store.flush, START + 86402)
    assert store.current("a")[0] == 40


@async_case
async def test_cancelled_publication_retains_io_ownership(tmp_path, monkeypatch):
    from tuner_under_test.history import commit

    store, staged, runtime, _replacement = prepared(tmp_path)
    entered, release = threading.Event(), threading.Event()
    original = Path.replace

    def held(path, target):
        if path == staged[0][3]:
            entered.set()
            assert release.wait(5)
        return original(path, target)

    monkeypatch.setattr(Path, "replace", held)
    work = asyncio.create_task(commit.publish(runtime, staged, lambda: True, lambda: None))
    assert await asyncio.to_thread(entered.wait, 5)
    work.cancel()
    await asyncio.sleep(0)
    assert not work.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await work
    assert store.day("a", DATE).vector(0)[0] == 20


@async_case
async def test_publication_failure_preserves_pending_data_and_refuses_more(tmp_path, monkeypatch):
    from tuner_under_test.history import commit

    store, staged, runtime, replacement = prepared(tmp_path)
    original = Path.replace

    def fail(path, target):
        if path == staged[0][3]:
            raise OSError("disk offline")
        return original(path, target)

    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(OSError, match="disk offline"):
        await commit.publish(runtime, staged, lambda: True, lambda: None)
    assert store.day("a", DATE) is replacement
    assert staged[0][3].exists()
    with pytest.raises(ValueError, match="publication"):
        store.reserve([])


@async_case
async def test_pending_worker_snapshot_reads_logically_accepted_day(tmp_path):
    store, staged, _runtime, _replacement = prepared(tmp_path)
    store.reserve(staged)
    snapshot = await asyncio.to_thread(store.worker_snapshot, "a", DATE)
    assert snapshot.file is None
    assert snapshot.day.vector(0)[0] == 20


@async_case
async def test_clear_disk_failure_keeps_history_hidden_and_is_reported(tmp_path, monkeypatch):
    import shutil

    from tuner_under_test.history import commit

    store, _staged, runtime, _replacement = prepared(tmp_path)

    def fail(*_args, **_kwargs):
        raise OSError("folder busy")

    monkeypatch.setattr(shutil, "rmtree", fail)
    with pytest.raises(OSError, match="folder busy"):
        await commit.clear_device(runtime, "a")
    assert store.day("a", DATE) is None
    with pytest.raises(OSError, match="folder busy"):
        await commit.drain(runtime)


@async_case
async def test_failed_metadata_acceptance_releases_unpublished_reservation(tmp_path):
    from tuner_under_test.history import commit

    store, staged, runtime, _replacement = prepared(tmp_path)

    def fail():
        raise ValueError("metadata refused")

    with pytest.raises(ValueError, match="metadata refused"):
        await commit.publish(runtime, staged, lambda: True, fail)
    assert store.day("a", DATE).vector(0)[0] == 10
    assert not staged[0][3].exists()
    assert store.reserve([])


@async_case
async def test_new_recording_rollover_remains_visible_while_old_clear_waits(tmp_path):
    store, _staged, _runtime, _replacement = prepared(tmp_path)
    store.forget_device("a")
    store.append("a", dayfile.Change(START + 86401, {0: 40}))
    store.append("a", dayfile.Change(START + 172801, {0: 50}))
    date = DATE + daystore.DAY
    assert store.day("a", date).vector(0)[0] == 40
    assert date in store.days("a")


def test_sync_clear_compatibility_removes_disk_without_event_loop(tmp_path):
    from tuner_under_test.history import commit

    store, _staged, runtime, _replacement = prepared(tmp_path)
    assert commit.clear_device(runtime, "a") is None
    assert not store.folder("a").exists()


def test_pending_deletion_hides_date_before_filesystem_publication(tmp_path):
    store, _staged, _runtime, _replacement = prepared(tmp_path)
    empty = dayfile.DayBuilder(*daystore.bounds(DATE)).day()
    staged = store.stage([("a", DATE, empty)])
    reservation = store.reserve(staged)
    assert store.day("a", DATE) is None
    assert DATE not in store.days("a")
    store.publish_reserved(reservation)
    assert not store._path("a", DATE, ".day").exists()


def test_pending_day_cannot_be_reserved_twice_or_evicted(tmp_path):
    store, staged, _runtime, replacement = prepared(tmp_path)
    store.reserve(staged)
    with pytest.raises(ValueError, match="already pending"):
        store.reserve(staged)
    store._cache_bytes = 0
    store._remember(("b", DATE), replacement)
    assert DATE in store.days("a")
    assert store.day("a", DATE) is replacement


@async_case
async def test_pending_snapshot_still_enforces_bounds_and_clear_hides_old_files(tmp_path):
    from tuner_under_test.history import worker_snapshot

    store, staged, _runtime, _replacement = prepared(tmp_path)
    store.reserve(staged)
    with pytest.raises(worker_snapshot.SnapshotTooLarge):
        await asyncio.to_thread(
            store.worker_snapshot, "a", DATE, limits=worker_snapshot.Limits(max_rows=0)
        )
    store.forget_device("a")
    snapshot = await asyncio.to_thread(store.worker_snapshot, "a", DATE)
    assert snapshot.file is None
    assert len(snapshot.day.times) == 0


@async_case
async def test_repeated_clear_requests_share_one_owned_disk_task(tmp_path):
    from tuner_under_test.history import commit

    store, _staged, runtime, _replacement = prepared(tmp_path)
    tasks = [commit.clear_device(runtime, "a") for _ in range(100)]
    assert len(set(tasks)) == 1
    assert len(runtime._storage_io_tasks) == 1
    await commit.drain(runtime)
    assert not store.folder("a").exists()
