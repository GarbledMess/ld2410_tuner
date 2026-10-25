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
