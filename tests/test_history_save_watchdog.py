"""Synthetic history save stalls remain visible until a flush succeeds."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import test_tuner
from tuner_under_test.history import dayfile
from tuner_under_test.history.daystore import DayStore
from tuner_under_test.worker import flushing


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr("time.time", lambda: 1792490400.0)
    result = test_tuner.mod.TunerRuntime(SimpleNamespace(), None, {"devices": {}})
    result._days = DayStore(tmp_path / "history")
    return result


def pending(runtime):
    runtime._days.append("synthetic", dayfile.Change(1792490401.0, {0: 20}))


def tick(runtime, monkeypatch, now):
    monkeypatch.setattr("time.time", lambda: now)
    runtime.sample_devices()


def test_stall_warns_once_with_bounded_diagnostics_and_resumes(runtime, monkeypatch, caplog):
    pending(runtime)
    tick(runtime, monkeypatch, 1792490402.0)
    tick(runtime, monkeypatch, 1792491301.0)  # A long Learn can delay a save this long.
    assert not caplog.records
    tick(runtime, monkeypatch, 1792491302.0)
    assert "Recorded history has not been saved for 15 min" in caplog.text
    warning = caplog.records[-1].getMessage()
    assert "worker" in warning and "flush task" in warning and "thread" in warning
    assert len(warning.encode()) <= 20480
    tick(runtime, monkeypatch, 1792491362.0)
    assert len(caplog.records) == 1
    runtime._days.flush(1792491362.0)
    tick(runtime, monkeypatch, 1792491363.0)
    assert "Recorded history saving resumed after 16 min" in caplog.text
    assert runtime._history_waiting_since is None
    assert runtime._history_saved_at == 1792491363.0
    pending(runtime)
    tick(runtime, monkeypatch, 1792491364.0)
    tick(runtime, monkeypatch, 1792492264.0)
    assert len(caplog.records) == 3


def test_idle_never_warns(runtime, monkeypatch, caplog):
    tick(runtime, monkeypatch, 1792490400.0)
    tick(runtime, monkeypatch, 1792494000.0)
    assert not caplog.records
    assert runtime._history_waiting_since is None
    assert runtime._history_saved_at == 1792494000.0


def test_completed_flush_resets_pending_age_even_when_new_records_remain(runtime, monkeypatch):
    async def run():
        pending(runtime)
        tick(runtime, monkeypatch, 1792490402.0)
        runtime.hass.async_add_executor_job = AsyncMock()

        async def drain(_runtime):
            return 1

        monkeypatch.setattr(flushing, "_drain", drain)
        monkeypatch.setattr("time.time", lambda: 1792490600.0)
        assert await flushing.flush(runtime) == 1
        assert runtime._history_saved_at == 1792490600.0
        assert runtime._history_waiting_since == 1792490600.0

    asyncio.run(run())


def test_stuck_task_diagnostics_include_stack_and_worker(runtime, monkeypatch, caplog):
    async def run():
        from tuner_under_test.worker.supervisor import Supervisor

        runtime._worker_service = Supervisor(__file__)
        pending(runtime)
        runtime._flush_task = asyncio.create_task(
            asyncio.Event().wait(), name="synthetic stuck flush"
        )
        await asyncio.sleep(0)
        try:
            tick(runtime, monkeypatch, 1792490402.0)
            tick(runtime, monkeypatch, 1792491302.0)
            assert "pending" in caplog.text
            assert "wait" in caplog.text
            assert "foreground" in caplog.text
        finally:
            runtime._flush_task.cancel()
            await asyncio.gather(runtime._flush_task, return_exceptions=True)

    asyncio.run(run())


def test_stalled_diagnostics_name_a_quarantined_worker_and_the_storage_lock(runtime, monkeypatch):
    from tuner_under_test.runtime import history_save
    from tuner_under_test.worker.supervisor import CleanupFailed, Supervisor

    async def run():
        runtime._worker_service = Supervisor(__file__)
        runtime._worker_service._closed = True
        runtime._worker_service._failure = CleanupFailed("synthetic")
        await runtime._storage_lock.acquire()
        report = history_save.diagnostics(runtime)
        assert "'closed': True" in report
        assert "CleanupFailed" in report
        assert "storage lock: held" in report

    asyncio.run(run())


def test_stalled_diagnostics_follow_the_awaited_chain(runtime):
    from tuner_under_test.runtime import history_save

    async def innermost_wait():
        await asyncio.Event().wait()

    async def outer_wait():
        await innermost_wait()

    async def run():
        task = asyncio.create_task(outer_wait(), name="synthetic flush")
        await asyncio.sleep(0)
        try:
            assert "in innermost_wait" in history_save._task_details(task)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_a_failing_flush_is_logged_once_and_cleared_by_success(runtime, monkeypatch, caplog):
    async def run():
        attempts = []

        async def fail(_runtime):
            attempts.append(1)
            raise ValueError("synthetic flush failure")

        monkeypatch.setattr(flushing, "flush", fail)
        monkeypatch.setattr("tuner_under_test.runtime.coordinator.STORE_DELAY", 0)
        await runtime._delayed_flush()
        await runtime._delayed_flush()
        assert len(attempts) == 2
        assert caplog.text.count("synthetic flush failure") == 2  # The message and its traceback.
        assert len(caplog.records) == 1
        from tuner_under_test.runtime import history_save

        history_save.completed(runtime)
        await runtime._delayed_flush()
        assert len(caplog.records) == 2

    asyncio.run(run())


def test_flush_writes_directly_when_the_worker_is_quarantined(runtime, monkeypatch, caplog):
    from tuner_under_test.worker.supervisor import Closed

    async def run():
        async def closed(_runtime):
            raise Closed("Worker supervisor is closed")

        async def executor(function, *args):
            return function(*args)

        monkeypatch.setattr(flushing, "_drain", closed)
        runtime.hass.async_add_executor_job = executor
        pending(runtime)
        monkeypatch.setattr("time.time", lambda: 1792490402.0)
        await flushing.flush(runtime)
        assert not flushing._pending(runtime._days)
        assert runtime._history_saved_at == 1792490402.0

    asyncio.run(run())
    assert "saving recorded history directly" in caplog.text


def test_flush_does_not_write_directly_once_shutdown_has_closed_workers(runtime, monkeypatch):
    from tuner_under_test.worker.supervisor import Closed

    async def run():
        async def closed(_runtime):
            raise Closed("Tuner numerical workers are stopped")

        monkeypatch.setattr(flushing, "_drain", closed)
        runtime._worker_closed = True
        pending(runtime)
        with pytest.raises(Closed):
            await flushing.flush(runtime)
        assert flushing._pending(runtime._days)

    asyncio.run(run())
