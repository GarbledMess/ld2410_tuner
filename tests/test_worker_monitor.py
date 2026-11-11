"""Read-only status for the worker lanes shown by the panel."""

import asyncio
from types import SimpleNamespace

import pytest
import test_tuner  # noqa: F401
from tuner_under_test.worker.supervisor import Supervisor


@pytest.fixture
def script(tmp_path):
    path = tmp_path / "worker.py"
    path.write_text("print('ok')\n")
    return path


def test_status_shows_named_active_operation_and_queue(script):
    async def run():
        worker = Supervisor(script)
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocked():
            entered.set()
            await release.wait()
            return ()

        first = asyncio.create_task(worker.run_when_available(blocked, operation="learn"))
        await entered.wait()
        second = asyncio.create_task(
            worker.run_when_available(
                lambda: asyncio.sleep(0, result=()), operation="charts", background=True
            )
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        state = worker.status()
        assert state["active"]["operation"] == "learn"
        assert state["active"]["stage"] == "preparing"
        assert state["active"]["eta_seconds"] is None
        assert state["active"]["progress_percent"] is None
        assert state["background"] == 1
        release.set()
        await asyncio.gather(first, second)
        assert worker.status()["active"] is None
        await worker.aclose()

    asyncio.run(run())


def test_eta_uses_bounded_successful_same_operation_history(script):
    async def run():
        worker = Supervisor(script)
        clock = [100.0]
        worker._clock = lambda: clock[0]

        async def completed(prepare, _finish, _consume):
            await prepare()
            clock[0] += 20
            return b"ok"

        worker._owned = completed
        for _ in range(2):
            await worker.run(lambda: asyncio.sleep(0, result=()), operation="learn")
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocked():
            entered.set()
            await release.wait()
            return ()

        task = asyncio.create_task(worker.run_when_available(blocked, operation="learn"))
        await entered.wait()
        clock[0] += 5
        assert worker.status()["active"]["eta_seconds"] == 15
        clock[0] += 20
        assert worker.status()["active"]["eta_seconds"] is None
        assert worker.status()["active"]["eta_state"] == "past_estimate"
        release.set()
        await task
        await worker.aclose()

    asyncio.run(run())


def test_snapshot_lists_all_current_lanes():
    from tuner_under_test.presentation.snapshots import worker_status

    service = SimpleNamespace(status=lambda: {"active": {"operation": "flush"}})
    runtime = SimpleNamespace(_storage_service=service)
    state = worker_status(runtime)
    assert state["storage"]["active"]["operation"] == "flush"
    assert state["calculations"]["active"] is None
    assert state["display"]["active"] is None
