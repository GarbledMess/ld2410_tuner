"""Foreground turns precede deferred background work without preemption or copies."""

import asyncio

import pytest
import test_tuner  # noqa: F401
from tuner_under_test.worker import supervisor


def test_foreground_overtakes_already_queued_background(monkeypatch):
    async def run():
        service = supervisor.Supervisor("unused")
        entered, release = asyncio.Event(), asyncio.Event()
        order = []

        async def process(args):
            if args == ("active",):
                entered.set()
                await release.wait()
            return args[0].encode()

        def prepare(name):
            async def admitted():
                order.append(name)
                return (name,)

            return admitted

        monkeypatch.setattr(service, "_process", process)
        active = asyncio.create_task(service.run(prepare("active")))
        await entered.wait()
        background = asyncio.create_task(
            service.run_when_available(prepare("background"), background=True)
        )
        await asyncio.sleep(0)
        foreground = asyncio.create_task(service.run_when_available(prepare("foreground")))
        await asyncio.sleep(0)
        assert order == ["active"]
        assert not active.done() and not background.done() and not foreground.done()
        release.set()
        assert await foreground == b"foreground"
        assert await background == b"background"
        assert await active == b"active"
        assert order == ["active", "foreground", "background"]
        await service.aclose()

    asyncio.run(run())


def test_deferred_cancellation_and_shutdown_never_prepare_waiters(monkeypatch):
    async def run():
        service = supervisor.Supervisor("unused")
        entered = asyncio.Event()
        prepared = []

        async def process(args):
            entered.set()
            await asyncio.Event().wait()

        async def prepare():
            prepared.append("active")
            return ()

        async def forbidden():
            pytest.fail("queued work prepared before its turn")

        monkeypatch.setattr(service, "_process", process)
        active = asyncio.create_task(service.run(prepare))
        await entered.wait()
        cancelled = asyncio.create_task(service.run_when_available(forbidden, background=True))
        waiting = asyncio.create_task(service.run_when_available(forbidden))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        await service.aclose()
        results = await asyncio.gather(active, waiting, return_exceptions=True)
        assert all(isinstance(result, asyncio.CancelledError) for result in results)
        assert prepared == ["active"]
        assert not service._queue and service._active is None
        with pytest.raises(supervisor.Closed):
            await service.run_when_available(forbidden)

    asyncio.run(run())


def test_nightly_and_periodic_workers_are_background_and_manual_calls_are_not():
    from types import SimpleNamespace
    from unittest.mock import patch

    from tuner_under_test.runtime import coordinator, schedule
    from tuner_under_test.worker import priority

    async def run():
        seen = []

        async def saved():
            seen.append(priority.is_background())

        owner = SimpleNamespace(async_save=saved, _save_dirty=True)
        with patch.object(coordinator, "STORE_DELAY", 0):
            await coordinator.TunerRuntime._delayed_save(owner)
        assert seen == [True]
        owner.data = {"devices": {}}

        async def refreshed(*args, **kwargs):
            seen.append(priority.is_background("references"))

        with patch.object(schedule.rebuilds, "refresh", refreshed):
            await schedule._run(owner, "2026-10-20")
        assert seen == [True, True, True, True]
        assert not priority.is_background()

        @priority.background
        async def nesting():
            assert priority.is_background("learn")
            with priority.interactive():
                assert not priority.is_background("references")
            assert priority.is_background()

        await nesting()
        assert not priority.is_background("learn")
        assert priority.is_background("references")

    asyncio.run(run())
