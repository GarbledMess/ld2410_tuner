"""Synthetic-only in-memory summaries and legacy sidecar cleanup."""

import asyncio
from unittest.mock import patch

import pytest
import test_tuner  # noqa: F401
from test_day_summaries import synthetic_day
from tuner_under_test.history import daystore, summaries, summary_cache


def installed(tmp_path, seed=27):
    store = daystore.DayStore(tmp_path / "history")
    day = synthetic_day(seed)
    date = daystore.date_of(day.start)
    store.replace("synthetic", date, day)
    return store, date


def test_restart_rebuilds_exact_index_without_persisting(tmp_path):
    store, date = installed(tmp_path)
    first = store.summary("synthetic", date)
    restarted = daystore.DayStore(store.root)
    loaded = restarted.summary("synthetic", date)
    assert not store._path("synthetic", date, ".sum").exists()
    for low, high in ((-1, 500), (2.25, 170.7), (449, 460)):
        assert loaded.histograms(low, high) == first.histograms(low, high)
        assert loaded.observed(low, high) == first.observed(low, high)
        assert list(loaded.coverage_ranges(low, high)) == list(first.coverage_ranges(low, high))
    assert loaded.day is restarted.day("synthetic", date)


def test_corruption_and_stale_metadata_rebuild(tmp_path):
    store, date = installed(tmp_path)
    expected = store.summary("synthetic", date).histograms(0, 500)
    cache = store._path("synthetic", date, ".sum")
    for content in (b"garbage", b"x" * 1000):
        cache.write_bytes(content)
        assert (
            daystore.DayStore(store.root).summary("synthetic", date).histograms(0, 500) == expected
        )
    physical = store._path("synthetic", date, ".day")
    stamp = physical.stat()
    import os

    os.utime(physical, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1000000))
    assert daystore.DayStore(store.root).summary("synthetic", date).histograms(0, 500) == expected


def test_generic_day_never_accesses_persistent_summary_and_loop_rejects_worker_api(tmp_path):
    store, date = installed(tmp_path)
    with patch.object(summaries, "of_day", side_effect=AssertionError("summary build")):
        assert daystore.DayStore(store.root).day("synthetic", date) is not None

    async def on_loop():
        with pytest.raises(RuntimeError, match="executor"):
            store.summary("synthetic", date)

    asyncio.run(on_loop())


def test_rewrite_commit_and_delete_remove_auxiliary_files(tmp_path):
    store, date = installed(tmp_path)
    cache = store._path("synthetic", date, ".sum")
    cache.write_bytes(b"legacy")
    store.replace("synthetic", date, synthetic_day(28))
    assert not cache.exists()
    cache.write_bytes(b"legacy")
    staged = store.stage([("synthetic", date, synthetic_day(29))])
    store.commit(staged)
    assert not cache.exists()
    cache.write_bytes(b"legacy")
    store.delete("synthetic", [date])
    assert not cache.exists()


def test_prune_removes_all_legacy_summaries_without_touching_physical_history(tmp_path):
    store, date = installed(tmp_path)
    paths = []
    for i in range(3):
        key = f"synthetic-{i}"
        store.replace(key, date, synthetic_day())
        paths.append(store._path(key, date, ".sum"))
        paths[-1].write_bytes(b"legacy")
    summary_cache.prune(store.root)
    assert not any(path.exists() for path in paths)
    assert all(path.with_suffix(".day").exists() for path in paths)


def test_live_snapshot_never_persists_an_index(tmp_path):
    from tuner_under_test.history import dayfile

    store = daystore.DayStore(tmp_path / "history")
    store.append("synthetic", dayfile.Change(1, {0: 10}))
    summary = store.summary("synthetic", daystore.date_of(1), until=20)
    assert summary.observed(0, 30) == 19
    assert list(store.root.rglob("*.sum")) == []


def test_failed_atomic_write_cannot_break_query_or_leave_temporary_file(tmp_path):
    store, date = installed(tmp_path)
    with patch("os.replace", side_effect=OSError("disk failure")):
        assert store.summary("synthetic", date).observed(0, 500) > 0
    assert list(store.root.rglob("*.sumtmp")) == []
    assert store._path("synthetic", date, ".day").exists()


def test_prune_removes_interrupted_auxiliary_writes_only(tmp_path):
    store, date = installed(tmp_path)
    orphan = store.root / "interrupted.sumtmp"
    orphan.write_bytes(b"partial compressed index")
    summary_cache.prune(store.root)
    assert not orphan.exists()
    assert store._path("synthetic", date, ".day").exists()


def test_locked_optional_sidecar_does_not_prevent_physical_delete(tmp_path):
    from pathlib import Path

    store, date = installed(tmp_path)
    store._path("synthetic", date, ".sum").write_bytes(b"legacy")
    unlink = Path.unlink

    def locked_sum(path, *args, **kwargs):
        if path.suffix == ".sum":
            raise PermissionError("optional file locked")
        return unlink(path, *args, **kwargs)

    with patch.object(Path, "unlink", locked_sum):
        store.delete("synthetic", [date])
    assert store.day("synthetic", date) is None


def test_prune_tolerates_disappearing_and_locked_auxiliary_files(tmp_path):
    from pathlib import Path

    store, date = installed(tmp_path)
    path = store._path("synthetic", date, ".sum")
    path.write_bytes(b"legacy")
    stat, unlink = Path.stat, Path.unlink

    def disappeared(found, *args, **kwargs):
        if found.suffix == ".sum":
            raise FileNotFoundError("another worker evicted it")
        return stat(found, *args, **kwargs)

    with patch.object(Path, "stat", disappeared):
        assert summary_cache._cache_files(store.root) == []

    def locked(found, *args, **kwargs):
        if found.suffix == ".sum":
            raise PermissionError("auxiliary file locked")
        return unlink(found, *args, **kwargs)

    with patch.object(Path, "unlink", locked):
        summary_cache.prune(store.root, max_bytes=0)
    assert path.exists()
    assert path.with_suffix(".day").exists()


def test_failed_temporary_cleanup_is_recovered_on_next_prune(tmp_path):
    from pathlib import Path

    store, date = installed(tmp_path)
    temporary = store.root / "interrupted.sumtmp"
    temporary.write_bytes(b"partial")
    with patch.object(Path, "unlink", side_effect=OSError("temporary locked")):
        summary_cache.prune(store.root)
    assert temporary.exists()
    assert store.summary("synthetic", date).observed(0, 500) > 0
    summary_cache.prune(store.root)
    assert not temporary.exists()


def test_startup_removes_legacy_summaries_without_a_result_write(tmp_path):
    store, date = installed(tmp_path)
    cache = store._path("synthetic", date, ".sum")
    cache.write_bytes(b"legacy")
    cache.with_suffix(".sumtmp").write_bytes(b"partial")
    result = cache.with_suffix(".res")
    result.write_bytes(b"retained result")
    restarted = daystore.DayStore(store.root)
    restarted.open(86401)
    assert not cache.exists()
    assert not cache.with_suffix(".sumtmp").exists()
    assert result.read_bytes() == b"retained result"
    assert restarted.day("synthetic", date) is not None


def test_missing_day_has_no_summary(tmp_path):
    store, date = installed(tmp_path)
    assert store.summary("absent", date) is None
