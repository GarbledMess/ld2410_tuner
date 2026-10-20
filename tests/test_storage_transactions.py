"""Owned storage jobs never publish unfinished worker output."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import test_tuner  # noqa: F401
from tuner_under_test.worker import supervisor


def test_consumer_runs_after_reap_before_directory_cleanup_and_holds_admission():
    async def run():
        service = supervisor.Supervisor(Path(__file__), max_pending=0)
        order = []

        async def prepare():
            return ()

        async def process(args):
            order.append("reaped")
            return b"result"

        async def consume(raw):
            assert raw == b"result"
            with pytest.raises(supervisor.Busy):
                await service.run(prepare)
            order.append("accepted")
            return 7

        async def finish():
            order.append("cleaned")

        with patch.object(service, "_process", process):
            assert await service.run(prepare, consume=consume, finish=finish) == 7
        assert order == ["reaped", "accepted", "cleaned"]
        await service.aclose()

    asyncio.run(run())


def test_private_tree_copy_refuses_symlinks_and_size_overruns(tmp_path, monkeypatch):
    from tuner_under_test.worker import transactions

    original, private = tmp_path / "original", tmp_path / "private"
    original.mkdir()
    (original / "record.day").write_bytes(b"recording")
    monkeypatch.setattr(transactions, "TREE_BYTES", 4)
    with pytest.raises(ValueError, match="budget"):
        transactions.copy_tree(original, private)
    assert (original / "record.day").read_bytes() == b"recording"


def test_transaction_failure_removes_private_tree_and_preserves_original(tmp_path):
    from tuner_under_test.worker import transactions

    async def run():
        root = tmp_path / "history"
        root.mkdir()
        (root / "record.day").write_bytes(b"recording")
        runtime = SimpleNamespace(
            data={"devices": {}},
            _days=SimpleNamespace(root=root),
            hass=SimpleNamespace(async_add_executor_job=asyncio.to_thread),
        )

        async def process(args):
            assert Path(args[0]).exists()
            raise ValueError("worker failed")

        async def accept(*args):
            pytest.fail("accepted failed worker")

        with patch.object(supervisor.Supervisor, "_process", side_effect=process):
            with pytest.raises(ValueError, match="worker failed"):
                await transactions.run(runtime, "maintenance", {}, accept)
        assert (root / "record.day").read_bytes() == b"recording"
        assert list(tmp_path.glob(".ld2410-storage-*")) == []
        from tuner_under_test.worker import bridge

        await bridge.stop(runtime)

    asyncio.run(run())


def test_storage_entrypoint_installs_limits_before_reading_request(tmp_path, monkeypatch):
    import importlib
    import io
    import sys

    from tuner_under_test.worker import entrypoint, protocol, transactions

    request = {
        "operation": "startup",
        "directory": str(tmp_path),
        "root": str(tmp_path / "history"),
        "data": {"devices": {}},
        "now": 2_000_000_000,
    }
    path = tmp_path / "request.json"
    path.write_bytes(transactions._message("learn", request))
    entrypoint._namespace()
    limits = importlib.import_module("ld2410_child.worker.limits")
    protected = []
    monkeypatch.setattr(limits, "install", lambda *args, **kwargs: protected.append(kwargs))
    child_transactions = importlib.import_module("ld2410_child.worker.transactions")
    original = child_transactions._read

    def read(*args):
        assert protected == [{"expected_parent": 123}]
        return original(*args)

    monkeypatch.setattr(child_transactions, "_read", read)
    output = io.BytesIO()
    monkeypatch.setattr(sys, "argv", ["entrypoint.py", str(path), "123", "storage"])
    monkeypatch.setattr(sys, "stdout", SimpleNamespace(buffer=output))
    entrypoint.main()
    assert protocol.unpack(output.getvalue(), "result") == {"storage": True}
    result = transactions._read(tmp_path / "result.json", "result")
    assert result["live"] == []


def test_storage_waits_for_bounded_busy_admission_without_dropping_pending_flush(tmp_path):
    from tuner_under_test.worker import protocol, transactions

    async def run():
        service = supervisor.Supervisor(Path(__file__), max_pending=0)
        active, release = asyncio.Event(), asyncio.Event()
        owner = SimpleNamespace(
            data={},
            _days=SimpleNamespace(root=tmp_path / "history"),
            _worker_service=service,
            hass=SimpleNamespace(async_add_executor_job=asyncio.to_thread),
        )

        async def prepare():
            return ()

        async def process(args):
            if not args:
                active.set()
                await release.wait()
                return b"active"
            directory = Path(args[0]).parent
            (directory / "result.json").write_bytes(protocol.pack("result", {"saved": True}))
            return protocol.pack("result", {"storage": True})

        async def accept(result, directory):
            return result

        with patch.object(service, "_process", process):
            running = asyncio.create_task(service.run(prepare))
            await active.wait()
            waiting = asyncio.create_task(transactions.run(owner, "flush", {}, accept))
            await asyncio.sleep(0)
            assert not waiting.done(), "background storage must retain its pending request"
            release.set()
            assert await running == b"active"
            assert await waiting == {"saved": True}
        await service.aclose()

    asyncio.run(run())
