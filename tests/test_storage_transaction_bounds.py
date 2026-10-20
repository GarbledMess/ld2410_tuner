"""Bounded filesystem acquisition and owned storage transaction failure paths."""

import asyncio
import os
import stat
import threading
from functools import wraps
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_tuner  # noqa: F401
from tuner_under_test.history import budget, daystore
from tuner_under_test.worker import bridge, protocol, supervisor, transactions


def async_case(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return run


def runtime_at(tmp_path):
    root = tmp_path / "history"
    root.mkdir()
    (root / "original.day").write_bytes(b"original history")
    return SimpleNamespace(
        data={"devices": {}},
        _days=daystore.DayStore(root),
        hass=SimpleNamespace(async_add_executor_job=asyncio.to_thread),
    )


def assert_original(runtime):
    assert (runtime._days.root / "original.day").read_bytes() == b"original history"


@pytest.mark.parametrize("limit", ["files", "entries", "depth"])
def test_snapshot_refuses_file_entry_and_depth_overruns(tmp_path, monkeypatch, limit):
    root = tmp_path / "history"
    root.mkdir()
    if limit == "files":
        (root / "a.day").write_bytes(b"a")
        (root / "b.day").write_bytes(b"b")
        monkeypatch.setattr(transactions, "TREE_FILES", 1)
    elif limit == "entries":
        for index in range(3):
            (root / str(index)).mkdir()
        monkeypatch.setattr(transactions, "TREE_FILES", 1)
    else:
        folder = root / "a" / "b"
        folder.mkdir(parents=True)
        (folder / "c.day").write_bytes(b"c")
    with pytest.raises(ValueError, match="budget"):
        transactions.copy_tree(root, tmp_path / "private")
    assert not (tmp_path / "private").exists()


def test_selected_snapshot_ignores_missing_and_preserves_zero_length_files(tmp_path):
    root = tmp_path / "history"
    (root / "a").mkdir(parents=True)
    (root / "a" / "empty.log").touch()
    (root / "unselected.day").write_bytes(b"not selected")
    private = tmp_path / "private"
    files = transactions.copy_tree(root, private, ["missing.day", "a/empty.log"])
    assert len(files) == 1
    assert (private / "a" / "empty.log").read_bytes() == b""
    assert not (private / "unselected.day").exists()


def test_snapshot_refuses_links_without_reading_target(tmp_path, monkeypatch):
    root = tmp_path / "history"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside private data")
    link = root / "link.day"
    if os.name == "posix":
        link.symlink_to(outside)
    else:
        # Symlink creation requires privileges on Windows; emulate only the
        # filesystem's link metadata query, keeping refusal logic real.
        link.write_bytes(b"placeholder")
        original = Path.is_symlink
        monkeypatch.setattr(Path, "is_symlink", lambda path: path == link or original(path))
    with pytest.raises(ValueError, match="links"):
        transactions.copy_tree(root, tmp_path / "private")
    assert outside.read_bytes() == b"outside private data"
    assert not (tmp_path / "private").exists()


def test_snapshot_refuses_nonregular_entries_without_opening_them(tmp_path, monkeypatch):
    root = tmp_path / "history"
    root.mkdir()
    fifo = root / "pipe.day"
    if hasattr(os, "mkfifo"):
        os.mkfifo(fifo)
    else:
        fifo.touch()
        original = Path.lstat

        def found(path):
            if path == fifo:
                return SimpleNamespace(st_mode=stat.S_IFIFO)
            return original(path)

        monkeypatch.setattr(Path, "lstat", found)
    with pytest.raises(ValueError, match="regular files"):
        transactions.copy_tree(root, tmp_path / "private")
    assert not (tmp_path / "private").exists()


@pytest.mark.parametrize("change", ["identity", "truncate", "grow"])
def test_snapshot_detects_source_change_instead_of_silently_copying(tmp_path, monkeypatch, change):
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"abc")
    expected = transactions._stamp(source.stat())
    if change == "identity":
        expected = (*expected[:-1], 99)
    else:
        original = os.fstat
        changed = False

        def found(fd):
            nonlocal changed
            value = original(fd)
            if not changed:
                changed = True
                source.write_bytes(b"" if change == "truncate" else b"abcdef")
            return value

        monkeypatch.setattr(os, "fstat", found)
    with pytest.raises(ValueError, match="changed during snapshot"):
        transactions._copy(source, target, expected)


@pytest.mark.parametrize("mode", ["acknowledgement", "result", "budget"])
@async_case
async def test_invalid_worker_output_cannot_reach_acceptance(tmp_path, monkeypatch, mode):
    runtime = runtime_at(tmp_path)

    async def process(_self, args):
        directory = Path(args[0]).parent
        if mode == "acknowledgement":
            return protocol.pack("result", {"storage": False})
        payload = (
            b"bad JSON"
            if mode == "result"
            else transactions._message("result", {"error": "metadata exceeds limit"})
        )
        (directory / "result.json").write_bytes(payload)
        return protocol.pack("result", {"storage": True})

    monkeypatch.setattr(supervisor.Supervisor, "_process", process)

    async def accept(*_args):
        pytest.fail("invalid output was accepted")

    error = budget.BudgetExceeded if mode == "budget" else ValueError
    with pytest.raises(error):
        await transactions.run(runtime, "maintenance", {}, accept)
    assert_original(runtime)
    assert list(tmp_path.glob(".ld2410-storage-*")) == []
    await bridge.stop(runtime)


@pytest.mark.parametrize("failure", ["snapshot", "metadata"])
@async_case
async def test_preparation_failure_releases_inputs_without_spawning(tmp_path, monkeypatch, failure):
    runtime = runtime_at(tmp_path)
    calls = []

    async def process(*_args):
        pytest.fail("invalid preparation spawned a worker")

    monkeypatch.setattr(supervisor.Supervisor, "_process", process)

    def snapshot(_directory):
        calls.append(1)
        raise ValueError("snapshot refused")

    async def accept(*_args):
        pytest.fail("invalid preparation was accepted")

    data = {}
    if failure == "metadata":
        monkeypatch.setattr(transactions, "MESSAGE_BYTES", 128)
        data = {"excess": "x" * 129}
    with pytest.raises(ValueError):
        await transactions.run(runtime, "maintenance", data, accept, snapshot=snapshot)
    assert calls == ([] if failure == "metadata" else [1])
    assert_original(runtime)
    assert list(tmp_path.glob(".ld2410-storage-*")) == []
    await bridge.stop(runtime)


def test_child_dispatch_rejects_unknown_operations_without_mutating_tree(tmp_path):
    marker = tmp_path / "original"
    marker.write_bytes(b"unchanged")
    with pytest.raises(ValueError, match="Unsupported storage operation"):
        transactions.execute({"operation": "arbitrary code", "directory": str(tmp_path)})
    assert marker.read_bytes() == b"unchanged"
    assert not (tmp_path / "result.json").exists()


def test_child_serializes_budget_refusal_and_reraises_other_value_errors(tmp_path):
    directory = tmp_path / "budget"
    directory.mkdir()
    request = {
        "operation": "maintenance",
        "root": str(directory / "history"),
        "directory": str(directory),
        "now": 2_000_000_000,
        "data": {"devices": {}, "storage_settings": {"max_mib": 0}},
    }
    assert protocol.unpack(transactions.execute(request), "result") == {"storage": True}
    result = transactions._read(directory / "result.json", "result")
    assert "size limit" in result["error"]
    folder = directory / "history" / "a"
    folder.mkdir(parents=True)
    (folder / "invalid.log").touch()
    with pytest.raises(ValueError, match="isoformat"):
        transactions.execute(request)


@async_case
async def test_failed_acceptance_retains_recovery_inputs_and_quarantines_service(
    tmp_path, monkeypatch
):
    runtime = runtime_at(tmp_path)
    directories = []

    async def process(_self, args):
        directory = Path(args[0]).parent
        directories.append(directory)
        (directory / "result.json").write_bytes(transactions._message("result", {"ready": True}))
        return protocol.pack("result", {"storage": True})

    monkeypatch.setattr(supervisor.Supervisor, "_process", process)

    async def accept(result, directory):
        assert result == {"ready": True}
        (directory / "recovery.marker").write_bytes(b"keep me")
        raise OSError("publication interrupted")

    with pytest.raises(supervisor.CleanupFailed):
        await transactions.run(runtime, "maintenance", {}, accept)
    assert (directories[0] / "recovery.marker").read_bytes() == b"keep me"
    with pytest.raises(supervisor.Closed):
        await transactions.run(runtime, "maintenance", {}, accept)
    with pytest.raises(supervisor.CleanupFailed):
        await bridge.stop(runtime)
    assert_original(runtime)


@async_case
async def test_cancelled_consumer_keeps_executor_and_inputs_owned_until_finished(
    tmp_path, monkeypatch
):
    runtime = runtime_at(tmp_path)
    entered, release, completed = threading.Event(), threading.Event(), threading.Event()
    directories = []

    async def process(_self, args):
        directory = Path(args[0]).parent
        directories.append(directory)
        (directory / "result.json").write_bytes(transactions._message("result", {}))
        return protocol.pack("result", {"storage": True})

    monkeypatch.setattr(supervisor.Supervisor, "_process", process)

    def blocking_io():
        entered.set()
        assert release.wait(5)
        assert directories[0].exists()
        completed.set()

    async def accept(_result, _directory):
        await supervisor.owned_preparation(asyncio.to_thread(blocking_io))

    work = asyncio.create_task(transactions.run(runtime, "maintenance", {}, accept))
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        work.cancel()
        with pytest.raises(asyncio.CancelledError):
            await work
        assert runtime._worker_service._jobs
        assert directories[0].exists()
        assert not completed.is_set()
    finally:
        release.set()
    with pytest.raises(supervisor.CleanupFailed):
        await bridge.stop(runtime)
    assert completed.is_set()
    assert directories[0].exists()
    assert_original(runtime)
