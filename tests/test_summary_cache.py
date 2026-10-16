"""Synthetic-only persistent physical indexes are optional and bounded."""

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


def test_restart_reuses_exact_index_without_rebuilding(tmp_path):
    store, date = installed(tmp_path)
    first = store.summary("synthetic", date)
    restarted = daystore.DayStore(store.root)
    with patch.object(summaries.Summary, "__init__", side_effect=AssertionError("rebuilt")):
        loaded = restarted.summary("synthetic", date)
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
    assert summary_cache.load(store.day("synthetic", date), physical) is None


def test_generic_day_never_accesses_persistent_summary_and_loop_rejects_worker_api(tmp_path):
    store, date = installed(tmp_path)
    with patch.object(summary_cache, "load", side_effect=AssertionError("cache I/O")):
        assert daystore.DayStore(store.root).day("synthetic", date) is not None

    async def on_loop():
        with pytest.raises(RuntimeError, match="executor"):
            store.summary("synthetic", date)

    asyncio.run(on_loop())


def test_rewrite_commit_and_delete_remove_auxiliary_files(tmp_path):
    store, date = installed(tmp_path)
    cache = store._path("synthetic", date, ".sum")
    store.summary("synthetic", date)
    store.replace("synthetic", date, synthetic_day(28))
    assert not cache.exists()
    store.summary("synthetic", date)
    staged = store.stage([("synthetic", date, synthetic_day(29))])
    store.commit(staged)
    assert not cache.exists()
    store.summary("synthetic", date)
    store.delete("synthetic", [date])
    assert not cache.exists()


def test_disk_cap_evicts_old_summaries_without_touching_physical_history(tmp_path):
    import os

    store, date = installed(tmp_path)
    paths = []
    for i in range(3):
        key = f"synthetic-{i}"
        store.replace(key, date, synthetic_day())
        store.summary(key, date)
        paths.append(store._path(key, date, ".sum"))
        os.utime(paths[-1], ns=(i + 1, i + 1))
    # Reading the oldest entry makes it most recently used.
    assert summary_cache.load(store.day("synthetic-0", date), paths[0].with_suffix(".day"))
    cap = sum(path.stat().st_size for path in (paths[0], paths[2]))
    with patch.object(summary_cache, "MAX_DISK_BYTES", cap):
        summary_cache.prune(store.root)
    assert paths[0].exists() and paths[2].exists()
    assert not paths[1].exists()
    assert sum(path.stat().st_size for path in store.root.rglob("*.sum")) <= cap
    assert all(path.with_suffix(".day").exists() for path in paths)


def test_oversized_compressed_payload_is_a_cache_miss(tmp_path):
    import zlib

    store, date = installed(tmp_path)
    physical = store._path("synthetic", date, ".day")
    physical.with_suffix(".sum").write_bytes(zlib.compress(b"a" * 2000))
    with patch.object(summary_cache, "MAX_RAW_BYTES", 1000):
        assert summary_cache.load(store.day("synthetic", date), physical) is None


def test_live_snapshot_never_persists_an_index(tmp_path):
    from tuner_under_test.history import dayfile

    store = daystore.DayStore(tmp_path / "history")
    store.append("synthetic", dayfile.Change(1, {0: 10}))
    summary = store.summary("synthetic", daystore.date_of(1), until=20)
    assert summary.observed(0, 30) == 19
    assert list(store.root.rglob("*.sum")) == []


def test_failed_atomic_write_cannot_break_query_or_leave_temporary_file(tmp_path):
    store, date = installed(tmp_path)
    with patch.object(summary_cache.os, "replace", side_effect=OSError("disk failure")):
        assert store.summary("synthetic", date).observed(0, 500) > 0
    assert list(store.root.rglob("*.sumtmp")) == []
    assert store._path("synthetic", date, ".day").exists()


def test_changed_day_during_encoding_is_not_published(tmp_path):
    import os

    store, date = installed(tmp_path)
    path = store._path("synthetic", date, ".day")
    summary = summaries.of_day(store.day("synthetic", date))
    identity = summary_cache.stamp(path)
    os.utime(path, ns=(identity[0], identity[0] + 1000000))
    summary_cache.store(summary, path, store.root, identity=identity)
    assert not path.with_suffix(".sum").exists()
    assert list(store.root.rglob("*.sumtmp")) == []


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
    store.summary("synthetic", date)
    unlink = Path.unlink

    def locked_sum(path, *args, **kwargs):
        if path.suffix == ".sum":
            raise PermissionError("optional file locked")
        return unlink(path, *args, **kwargs)

    with patch.object(Path, "unlink", locked_sum):
        store.delete("synthetic", [date])
    assert store.day("synthetic", date) is None


@pytest.mark.parametrize(
    "damage",
    [
        "header",
        "truncated",
        "array_length",
        "coverage",
        "gate_count",
        "bin_count",
        "energy",
        "stream",
        "trailing",
    ],
)
def test_well_compressed_but_invalid_indexes_are_rejected(tmp_path, damage):
    import copy
    import struct
    import zlib
    from array import array

    store, date = installed(tmp_path)
    day = store.day("synthetic", date)
    summary = copy.copy(summaries.of_day(day))
    path = store._path("synthetic", date, ".day")
    identity = summary_cache.stamp(path)
    if damage == "coverage":
        summary.known = bytearray([2]) * len(day.times)
    if damage == "energy":
        summary.gates = [{101: next(iter(summary.gates[0].values()))}, *summary.gates[1:]]
    if damage == "stream":
        stream = copy.copy(next(iter(summary.gates[0].values())))
        stream.starts = array("I", stream.ends)  # Empty runs must never be admitted.
        summary.gates = [{0: stream}, *summary.gates[1:]]
    raw = bytearray(zlib.decompress(summary_cache._encode(summary, identity)))
    if damage == "header":
        raw[0] ^= 1
    elif damage == "truncated":
        raw = raw[: summary_cache._HEADER.size]
    elif damage == "array_length":
        struct.pack_into("<I", raw, summary_cache._HEADER.size, 0xFFFFFFFF)
    elif damage in ("gate_count", "bin_count"):
        offset = summary_cache._HEADER.size
        for _ in range(5):
            length = struct.unpack_from("<I", raw, offset)[0]
            offset += 4 + length
        if damage == "bin_count":
            offset += 4
        struct.pack_into("<I", raw, offset, 102)
    elif damage == "trailing":
        raw.extend(b"unexpected extra field")
    path.with_suffix(".sum").write_bytes(zlib.compress(raw))
    assert summary_cache.load(day, path) is None


def test_write_admission_rejects_indexes_over_raw_or_compressed_budget(tmp_path):
    store, date = installed(tmp_path)
    path = store._path("synthetic", date, ".day")
    summary = summaries.of_day(store.day("synthetic", date))
    with patch.object(summary_cache, "MAX_RAW_BYTES", 1):
        summary_cache.store(summary, path, store.root)
    assert not path.with_suffix(".sum").exists()
    with patch.object(summary_cache, "MAX_DISK_BYTES", 1):
        summary_cache.store(summary, path, store.root)
    assert not path.with_suffix(".sum").exists()
    assert list(store.root.rglob("*.sumtmp")) == []


def test_read_admission_and_identity_changes_return_cache_miss(tmp_path):
    store, date = installed(tmp_path)
    day = store.day("synthetic", date)
    store.summary("synthetic", date)
    path = store._path("synthetic", date, ".day")
    identity = summary_cache.stamp(path)
    with patch.object(summary_cache, "MAX_DISK_BYTES", 1):
        assert summary_cache.load(day, path) is None
    changed = (identity[0] + 1, identity[1])
    assert summary_cache.load(day, path, identity=changed) is None
    with patch.object(summary_cache, "stamp", side_effect=[identity, changed]):
        assert summary_cache.load(day, path) is None


def test_prune_tolerates_disappearing_and_locked_auxiliary_files(tmp_path):
    from pathlib import Path

    store, date = installed(tmp_path)
    store.summary("synthetic", date)
    path = store._path("synthetic", date, ".sum")
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
    store, date = installed(tmp_path)
    with (
        patch.object(summary_cache.os, "replace", side_effect=OSError("disk failure")),
        patch.object(summary_cache.os, "unlink", side_effect=OSError("temporary locked")),
    ):
        assert store.summary("synthetic", date).observed(0, 500) > 0
    assert len(list(store.root.rglob("*.sumtmp"))) == 1
    summary_cache.prune(store.root)
    assert list(store.root.rglob("*.sumtmp")) == []
