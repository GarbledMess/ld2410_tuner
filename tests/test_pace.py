"""Non-vital background work is low priority: one job at a time, at the lowest priority the
platform allows, using at most ``pace.SHARE`` of the interpreter."""

import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import test_tuner  # noqa: F401
from tuner_under_test.history import pace


@pytest.fixture(autouse=True)
def governed():
    with patch.object(pace, "SHARE", 0.2):
        yield


def burn(seconds, steps):
    for _ in range(steps):
        deadline = time.thread_time() + seconds / steps
        while time.thread_time() < deadline:
            pass
        pace.rest()


@pace.background
def job(seconds=0.2, steps=10):
    started = time.thread_time()
    burn(seconds, steps)
    return time.thread_time() - started


def test_background_jobs_rest_four_times_as_long_as_they_work():
    slept = []
    with patch.object(pace.time, "sleep", side_effect=slept.append):
        worked = asyncio.run(asyncio.to_thread(job))
    assert slept
    # At most a fifth of the interpreter (thread CPU clocks tick in ~16 ms on Windows).
    assert abs(sum(slept) - 4 * worked) <= 4 * 0.035


def test_background_jobs_run_one_at_a_time():
    running, overlap = [0], []

    @pace.background
    def counted():
        running[0] += 1
        overlap.append(running[0])
        time.sleep(0.02)
        running[0] -= 1

    async def scenario():
        await asyncio.gather(*(asyncio.to_thread(counted) for _ in range(4)))

    asyncio.run(scenario())
    assert max(overlap) == 1


def test_the_event_loop_thread_and_undecorated_work_are_never_throttled():
    with patch.object(pace.time, "sleep", side_effect=AssertionError):
        job(0.1, 4)  # Called on the main thread: runs as is.
        asyncio.run(asyncio.to_thread(burn, 0.1, 4))  # Not background work.


def test_decorated_jobs_keep_their_identity():
    assert job.__name__ == "job"
    assert pace.background(job) is job  # Decorating twice changes nothing.


def test_idle_time_is_not_counted_as_work():
    slept = []

    @pace.background
    def idle():
        pace.rest()
        threading.Event().wait(0.3)
        pace.rest()

    with patch.object(pace.time, "sleep", side_effect=slept.append):
        asyncio.run(asyncio.to_thread(idle))
    assert slept == []


def test_stopping_abandons_running_work_and_later_work_still_runs():
    started = threading.Event()

    @pace.background
    def endless():
        started.set()
        while True:
            pace.rest()  # Abandoned here once the integration stops.
            time.sleep(0.001)

    async def scenario():
        running = asyncio.ensure_future(asyncio.to_thread(endless))
        await asyncio.to_thread(started.wait)
        pace.stop()
        with pytest.raises(pace.Stopped):
            await running
        assert await asyncio.to_thread(job, 0.01, 1) >= 0  # After a reload: runs normally.

    asyncio.run(scenario())


def test_jobs_wait_outside_the_shared_executor_and_stop_discards_waiters():
    async def scenario():
        entered, finish = asyncio.Event(), asyncio.Event()
        submitted = []

        async def execute(fn, *args):
            submitted.append(fn.__name__)
            entered.set()
            await finish.wait()
            return 42

        hass = SimpleNamespace(async_add_executor_job=execute)
        first = asyncio.create_task(pace.run(hass, job))
        await entered.wait()
        second = asyncio.create_task(pace.run(hass, burn, 0, 0))
        await asyncio.sleep(0)
        assert submitted == ["job"]
        pace.stop()
        finish.set()
        assert await first == 42
        with pytest.raises(pace.Stopped):
            await second
        assert await pace.run(hass, job) == 42

    asyncio.run(scenario())


def test_cancelled_caller_does_not_admit_another_worker_until_its_work_finishes():
    async def scenario():
        entered, finish = asyncio.Event(), asyncio.Event()
        submitted = []

        async def execute(fn, *args):
            submitted.append(fn.__name__)
            entered.set()
            await finish.wait()
            return 42

        hass = SimpleNamespace(async_add_executor_job=execute)
        first = asyncio.create_task(pace.run(hass, job))
        await entered.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(pace.run(hass, burn, 0, 0))
        await asyncio.sleep(0)
        assert submitted == ["job"]
        finish.set()
        assert await second == 42
        assert submitted == ["job", "burn"]

    asyncio.run(scenario())


def test_submission_failure_releases_admission():
    async def scenario():
        def broken(fn, *args):
            raise RuntimeError("executor unavailable")

        hass = SimpleNamespace(async_add_executor_job=broken)
        with pytest.raises(RuntimeError, match="executor unavailable"):
            await pace.run(hass, job)
        hass.async_add_executor_job = lambda fn, *args: asyncio.sleep(0, result=42)
        assert await pace.run(hass, job) == 42

    asyncio.run(scenario())


def test_cancelled_waiter_is_never_submitted():
    async def scenario():
        entered, finish = asyncio.Event(), asyncio.Event()
        submitted = []

        async def execute(fn, *args):
            submitted.append(fn.__name__)
            entered.set()
            await finish.wait()

        hass = SimpleNamespace(async_add_executor_job=execute)
        first = asyncio.create_task(pace.run(hass, job))
        await entered.wait()
        second = asyncio.create_task(pace.run(hass, burn, 0, 0))
        await asyncio.sleep(0)
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        finish.set()
        await first
        assert submitted == ["job"]

    asyncio.run(scenario())


def test_os_priority_changes_never_touch_home_assistants_shared_worker():
    priorities = []

    async def scenario():
        def shared_worker():
            worker_id = threading.get_ident()
            job(0, 0)
            return worker_id

        return await asyncio.to_thread(shared_worker)

    with patch.object(pace, "_lower", side_effect=lambda: priorities.append(threading.get_ident())):
        shared_id = asyncio.run(scenario())
    assert priorities
    assert shared_id not in priorities


def test_stop_discards_a_job_already_queued_in_home_assistants_executor():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        called = []

        @pace.background
        def mark():
            called.append(True)

        async def executor(fn, *args):
            entered.set()
            await release.wait()
            return await asyncio.to_thread(fn, *args)

        hass = SimpleNamespace(async_add_executor_job=executor)
        work = asyncio.create_task(pace.run(hass, mark))
        await entered.wait()
        pace.stop()
        release.set()
        with pytest.raises(pace.Stopped):
            await work
        assert called == []

    asyncio.run(scenario())


def test_stopping_a_reference_rebuild_is_not_reported_as_a_failure():
    from tuner_under_test.presence import rebuilds

    async def executor(fn, *args):
        raise pace.Stopped

    async def scenario():
        runtime = SimpleNamespace(hass=SimpleNamespace(async_add_executor_job=executor))
        with patch.object(rebuilds._LOGGER, "exception") as failure:
            with pytest.raises(pace.Stopped):
                await rebuilds._pass(runtime, [])
            failure.assert_not_called()

    asyncio.run(scenario())


def test_six_milliseconds_of_cpu_already_yields_to_home_assistant():
    @pace.background
    def checkpoint():
        pace.rest()

    with (
        patch.object(pace.time, "thread_time", side_effect=(0.0, 0.006, 0.006)),
        patch.object(pace.time, "sleep") as sleep,
    ):
        asyncio.run(asyncio.to_thread(checkpoint))
    sleep.assert_called_once_with(pytest.approx(0.024))
