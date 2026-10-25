"""Owned worker lifetime, bounded admission and output; synthetic processes only."""

import asyncio
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_tuner  # noqa: F401
from tuner_under_test.worker import supervisor


@pytest.fixture(autouse=True)
def windows_process_constant(monkeypatch):
    """Fake Windows spawn boundaries also need the Windows-only stdlib flag."""
    monkeypatch.setattr(supervisor.subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x4000, raising=False)


SCRIPT = r"""
import sys, time
sys.stdout.reconfigure(newline='\n')
mode = sys.argv[1]
if mode == 'ok':
    print('result')
elif mode == 'crash':
    print('failure', file=sys.stderr)
    sys.exit(23)
elif mode in ('stdout', 'stderr'):
    stream = getattr(sys, mode)
    while True:
        stream.write('x' * 4096)
        stream.flush()
elif mode == 'hang':
    time.sleep(60)
"""


@pytest.fixture
def script(tmp_path):
    path = tmp_path / "synthetic.py"
    path.write_text(SCRIPT)
    return path


def prepare(mode):
    async def ready():
        return (mode,)

    return ready


def test_real_output_exit_and_recovery(script):
    async def run():
        worker = supervisor.Supervisor(script)
        assert await worker.run(prepare("ok")) == b"result\n"
        with pytest.raises(supervisor.WorkerFailed) as failure:
            await worker.run(prepare("crash"))
        assert failure.value.returncode == 23
        assert b"failure" in failure.value.stderr
        assert await worker.run(prepare("ok")) == b"result\n"
        await worker.aclose()
        with pytest.raises(supervisor.Closed):
            await worker.run(prepare("ok"))

    asyncio.run(run())


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_excess_output_is_reaped_before_next_job(script, stream):
    async def run():
        worker = supervisor.Supervisor(script, stdout_bytes=128, stderr_bytes=128)
        with pytest.raises(supervisor.OutputLimit):
            await worker.run(prepare(stream))
        assert await worker.run(prepare("ok")) == b"result\n"
        await worker.aclose()

    asyncio.run(run())


def test_preparation_is_admitted_and_pending_work_is_bounded(script):
    async def run():
        worker = supervisor.Supervisor(script, max_pending=1)
        entered, release = asyncio.Event(), asyncio.Event()
        called = []

        async def blocked():
            entered.set()
            await release.wait()
            return ("ok",)

        async def queued():
            called.append(True)
            return ("ok",)

        first = asyncio.create_task(worker.run(blocked))
        await entered.wait()
        second = asyncio.create_task(worker.run(queued))
        await asyncio.sleep(0)  # Let the second caller register; no timing race.
        with pytest.raises(supervisor.Busy):
            await worker.run(queued)
        assert not called
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        release.set()
        assert await first == b"result\n"
        assert not called
        await worker.aclose()

    asyncio.run(run())


def test_deadline_includes_preparation(script):
    async def run():
        worker = supervisor.Supervisor(script, deadline=0.01)
        with pytest.raises(TimeoutError):
            await worker.run(asyncio.Event().wait)
        await worker.aclose()

    asyncio.run(run())


def test_running_child_deadline(script):
    async def run():
        worker = supervisor.Supervisor(script, deadline=0.2)
        with pytest.raises(TimeoutError):
            await worker.run(prepare("hang"))
        await worker.aclose()

    asyncio.run(run())


def test_cancel_during_spawn_and_repeated_shutdown_preserve_ownership(script, monkeypatch):
    async def run():
        create = asyncio.create_subprocess_exec
        spawned, release = asyncio.Event(), asyncio.Event()
        children = []

        async def launch(*args, **kwargs):
            assert args[:4] == (sys.executable, "-I", "-S", "-B")
            assert Path(args[4]) == script
            child = await create(*args, **kwargs)
            children.append(child)
            spawned.set()
            await release.wait()
            return child

        monkeypatch.setattr(asyncio, "create_subprocess_exec", launch)
        worker = supervisor.Supervisor(script)
        request = asyncio.create_task(worker.run(prepare("hang")))
        await spawned.wait()
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        closing = asyncio.create_task(worker.aclose())
        await asyncio.sleep(0)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await closing
        release.set()
        await worker.aclose()
        assert len(children) == 1
        assert children[0].returncode is not None

    asyncio.run(run())


def test_shutdown_cancels_active_preparation_and_queued_calls(script):
    async def run():
        worker = supervisor.Supervisor(script)
        entered = asyncio.Event()
        called = []

        async def blocked():
            entered.set()
            await asyncio.Event().wait()

        async def queued():
            called.append(True)

        first = asyncio.create_task(worker.run(blocked))
        await entered.wait()
        second = asyncio.create_task(worker.run(queued))
        await asyncio.sleep(0)
        await worker.aclose()
        results = await asyncio.gather(first, second, return_exceptions=True)
        assert all(isinstance(r, asyncio.CancelledError) for r in results)
        assert not called

    asyncio.run(run())


@pytest.mark.parametrize(
    "option,value",
    [("max_pending", -1), ("deadline", 0), ("grace", 0), ("stdout_bytes", 0), ("stderr_bytes", 0)],
)
def test_invalid_bounds_are_rejected(script, option, value):
    with pytest.raises(ValueError):
        supervisor.Supervisor(script, **{option: value})


def test_cancelled_executor_preparation_keeps_admission_until_it_finishes(script):
    assert callable(getattr(supervisor, "owned_preparation", None))

    async def run():
        worker = supervisor.Supervisor(script)
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        called = []

        def copying():
            loop.call_soon_threadsafe(entered.set)
            release.wait()
            return ("ok",)

        async def copying_input():
            return await supervisor.owned_preparation(loop.run_in_executor(None, copying))

        async def next_input():
            called.append(True)
            return ("ok",)

        first = asyncio.create_task(worker.run(copying_input))
        try:
            await entered.wait()
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            second = asyncio.create_task(worker.run(next_input))
            await asyncio.sleep(0)
            assert not called
            release.set()
            assert await second == b"result\n"
        finally:
            release.set()
            await worker.aclose()

    asyncio.run(run())


class ControlledProcess:
    """OS-boundary stand-in for an unresponsive process, never an actual runaway."""

    pid = 123

    def __init__(self, *, unkillable=False, open_pipes=False):
        self.returncode = 0 if open_pipes else None
        self.unkillable = unkillable
        self.terminated, self.killed = asyncio.Event(), asyncio.Event()
        self.exited = asyncio.Event()
        self.stdout, self.stderr = asyncio.StreamReader(), asyncio.StreamReader()
        if not open_pipes:
            self.stdout.feed_eof()
            self.stderr.feed_eof()

    def terminate(self):
        self.terminated.set()

    def kill(self):
        self.killed.set()
        if not self.unkillable:
            self.returncode = -9
            self.exited.set()

    async def wait(self):
        if self.returncode is None:
            await self.exited.wait()
        return self.returncode


@pytest.mark.parametrize("problem", ["ignores_term", "unkillable", "open_pipes"])
def test_escalation_and_quarantine_at_os_boundary(script, monkeypatch, problem):
    async def run():
        child = ControlledProcess(
            unkillable=problem == "unkillable", open_pipes=problem == "open_pipes"
        )

        async def spawn(*args, **kwargs):
            return child

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(supervisor, "_POSIX", False)
        worker = supervisor.Supervisor(script, deadline=0.01, grace=0.01)
        error = TimeoutError if problem == "ignores_term" else supervisor.CleanupFailed
        with pytest.raises(error):
            await worker.run(prepare("hang"))
        if problem == "ignores_term":
            assert child.killed.is_set()
        else:
            with pytest.raises(supervisor.Closed):
                await worker.run(prepare("ok"))
        if problem == "ignores_term":
            await worker.aclose()
        else:
            with pytest.raises(supervisor.CleanupFailed):
                await worker.aclose()

    asyncio.run(run())


def test_cancellation_during_cleanup_does_not_interrupt_kill(script, monkeypatch):
    async def run():
        child = ControlledProcess()

        async def spawn(*args, **kwargs):
            return child

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(supervisor, "_POSIX", False)
        worker = supervisor.Supervisor(script, deadline=0.01, grace=0.01)
        request = asyncio.create_task(worker.run(prepare("hang")))
        await child.terminated.wait()
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        await worker.aclose()
        assert child.killed.is_set()

    asyncio.run(run())


def test_posix_signals_target_the_owned_process_group(monkeypatch):
    calls = []
    monkeypatch.setattr(supervisor, "_POSIX", True)
    monkeypatch.setattr(supervisor.signal, "SIGKILL", 9, raising=False)
    monkeypatch.setattr(supervisor.os, "killpg", lambda *a: calls.append(a), raising=False)
    child = SimpleNamespace(pid=123)
    supervisor._signal(child)
    supervisor._signal(child, force=True)
    assert calls == [(123, supervisor.signal.SIGTERM), (123, 9)]

    def gone(*args):
        raise ProcessLookupError

    monkeypatch.setattr(supervisor.os, "killpg", gone)
    supervisor._signal(child)


def test_failed_spawn_and_preparation_do_not_poison_later_jobs(script, monkeypatch):
    async def run():
        original = asyncio.create_subprocess_exec
        worker = supervisor.Supervisor(script)

        async def fail(*args, **kwargs):
            raise OSError("synthetic spawn failure")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", fail)
        with pytest.raises(OSError, match="synthetic spawn"):
            await worker.run(prepare("ok"))
        monkeypatch.setattr(asyncio, "create_subprocess_exec", original)

        async def invalid():
            return ["not a tuple"]

        with pytest.raises(ValueError, match="tuple of strings"):
            await worker.run(invalid)
        assert await worker.run(prepare("ok")) == b"result\n"
        await worker.aclose()

    asyncio.run(run())


def test_failed_cleanup_rejects_an_already_queued_request(script, monkeypatch):
    async def run():
        child, spawned = ControlledProcess(unkillable=True), asyncio.Event()

        async def spawn(*args, **kwargs):
            spawned.set()
            return child

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(supervisor, "_POSIX", False)
        worker = supervisor.Supervisor(script, deadline=0.01, grace=0.01)
        first = asyncio.create_task(worker.run(prepare("hang")))
        await spawned.wait()
        second = asyncio.create_task(worker.run(prepare("ok")))
        with pytest.raises(supervisor.CleanupFailed):
            await first
        with pytest.raises(supervisor.Closed):
            await second
        with pytest.raises(supervisor.CleanupFailed):
            await worker.aclose()

    asyncio.run(run())


def test_unexpected_executor_cancellation_is_a_fatal_ownership_failure(script):
    async def run():
        work = asyncio.get_running_loop().create_future()
        work.cancel()
        worker = supervisor.Supervisor(script)

        async def cancelled():
            return await supervisor.owned_preparation(work)

        with pytest.raises(supervisor.CleanupFailed):
            await worker.run(cancelled)
        with pytest.raises(supervisor.Closed):
            await worker.run(prepare("ok"))
        with pytest.raises(supervisor.CleanupFailed):
            await worker.aclose()

    asyncio.run(run())


def test_repeated_preparation_cancellation_leaves_underlying_work_owned():
    async def run():
        work = asyncio.get_running_loop().create_future()
        owner = asyncio.create_task(supervisor.owned_preparation(work))
        await asyncio.sleep(0)
        owner.cancel()
        await asyncio.sleep(0)
        owner.cancel()
        await asyncio.sleep(0)
        assert not work.cancelled()
        work.set_result("finished")
        with pytest.raises(asyncio.CancelledError):
            await owner

    asyncio.run(run())


def test_posix_cleanup_kills_group_even_after_leader_exit(monkeypatch):
    async def run():
        calls = []
        monkeypatch.setattr(supervisor, "_POSIX", True)
        monkeypatch.setattr(supervisor.signal, "SIGKILL", 9, raising=False)
        monkeypatch.setattr(supervisor.os, "killpg", lambda *a: calls.append(a), raising=False)
        child = ControlledProcess(open_pipes=True)
        await supervisor._reap(child, 0.01)
        assert calls == [(child.pid, supervisor.signal.SIGTERM), (child.pid, 9)]

    asyncio.run(run())


@pytest.mark.parametrize("boundary", ["terminate", "kill", "wait", "drain"])
def test_os_cleanup_failure_quarantines_and_shutdown_reports_it(script, monkeypatch, boundary):
    async def run():
        child, spawned = ControlledProcess(), asyncio.Event()

        def refused():
            raise PermissionError("synthetic permission failure")

        if boundary in ("terminate", "kill"):
            setattr(child, boundary, refused)
        elif boundary == "wait":

            async def wait_failed():
                raise OSError("synthetic wait failure")

            child.wait = wait_failed
        else:
            reads = 0
            child.returncode = 0

            async def read_failed(_count):
                nonlocal reads
                reads += 1
                if reads > 1:
                    raise OSError("synthetic drain failure")
                return b""

            child.stdout.read = read_failed

        async def spawn(*args, **kwargs):
            spawned.set()
            return child

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(supervisor, "_POSIX", False)
        worker = supervisor.Supervisor(script, deadline=0.01, grace=0.01)
        first = asyncio.create_task(worker.run(prepare("hang")))
        await spawned.wait()
        second = asyncio.create_task(worker.run(prepare("ok")))
        with pytest.raises(supervisor.CleanupFailed):
            await first
        with pytest.raises(supervisor.Closed):
            await second
        for _ in range(2):
            with pytest.raises(supervisor.CleanupFailed):
                await worker.aclose()

    asyncio.run(run())


def test_shutdown_reports_cleanup_failure_after_the_caller_has_cancelled(script, monkeypatch):
    async def run():
        child, spawned = ControlledProcess(unkillable=True), asyncio.Event()

        async def spawn(*args, **kwargs):
            spawned.set()
            return child

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(supervisor, "_POSIX", False)
        worker = supervisor.Supervisor(script, grace=0.01)
        request = asyncio.create_task(worker.run(prepare("hang")))
        await spawned.wait()
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        for _ in range(2):
            with pytest.raises(supervisor.CleanupFailed):
                await worker.aclose()

    asyncio.run(run())


def test_owned_finalizer_holds_admission_until_files_are_released(script):
    async def run():
        worker = supervisor.Supervisor(script)
        entered, release = asyncio.Event(), asyncio.Event()
        called = []

        async def finish():
            entered.set()
            await release.wait()
            called.append("cleaned")

        first = asyncio.create_task(worker.run(prepare("ok"), finish=finish))
        await entered.wait()

        async def next_job():
            called.append("next")
            return ("ok",)

        second = asyncio.create_task(worker.run(next_job))
        await asyncio.sleep(0)
        assert called == []
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        assert called == []
        release.set()
        assert await second == b"result\n"
        assert called == ["cleaned", "next"]
        await worker.aclose()

    asyncio.run(run())


def test_preparation_failure_still_releases_owned_files(script):
    async def run():
        worker = supervisor.Supervisor(script)
        cleaned = []

        async def fail():
            raise ValueError("failed preparation")

        async def finish():
            cleaned.append(True)

        with pytest.raises(ValueError, match="preparation"):
            await worker.run(fail, finish=finish)
        assert cleaned == [True]
        await worker.aclose()

    asyncio.run(run())


def test_worker_failure_explains_the_bounded_child_refusal():
    error = supervisor.WorkerFailed(
        1, b"Worker refused: SnapshotTooLarge: Worker day budget exceeded"
    )
    assert "Worker day budget exceeded" in str(error)
    assert len(str(supervisor.WorkerFailed(1, b"x" * 100000))) < 2200


def test_failed_input_cleanup_quarantines_future_jobs(script):
    async def run():
        worker = supervisor.Supervisor(script)

        async def fail():
            raise OSError("cannot remove owned inputs")

        with pytest.raises(supervisor.CleanupFailed, match="input cleanup"):
            await worker.run(prepare("ok"), finish=fail)
        with pytest.raises(supervisor.Closed):
            await worker.run(prepare("ok"))
        with pytest.raises(supervisor.CleanupFailed):
            await worker.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("age", [0, 59.999, 60, 120])
def test_background_age_bounds_foreground_priority(script, monkeypatch, age):
    """A continuous stream gets priority only until durability's maximum wait."""

    async def run():
        worker = supervisor.Supervisor(script)
        clock = [100.0]
        worker._clock = lambda: clock[0]
        worker.BACKGROUND_MAX_WAIT = 60
        entered, release = asyncio.Event(), asyncio.Event()
        order = []

        async def process(_args):
            return b"result"

        monkeypatch.setattr(worker, "_process", process)

        async def blocked():
            entered.set()
            await release.wait()
            return ()

        def job(name):
            async def ready():
                order.append(name)
                return ()

            return ready

        first = asyncio.create_task(worker.run(blocked))
        await entered.wait()
        background = asyncio.create_task(
            worker.run_when_available(job("background"), background=True)
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        clock[0] += age
        foreground = [
            asyncio.create_task(worker.run_when_available(job(f"foreground-{index}")))
            for index in range(5)
        ]
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(first, background, *foreground)
        expected = [f"foreground-{index}" for index in range(5)]
        expected.insert(0 if age >= 60 else 5, "background")
        assert order == expected
        await worker.aclose()

    asyncio.run(run())


def test_supervisor_diagnostics_report_active_and_waiting_age(script, monkeypatch):
    async def run():
        worker = supervisor.Supervisor(script)
        clock = [100.0]
        worker._clock = lambda: clock[0]
        entered = asyncio.Event()

        async def blocked():
            entered.set()
            await asyncio.Event().wait()

        first = asyncio.create_task(worker.run_when_available(blocked, background=True))
        await entered.wait()
        queued = [
            asyncio.create_task(worker.run_when_available(blocked, background=background))
            for background in (True, False)
        ]
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        clock[0] += 75
        assert worker.diagnostics() == {
            "active": {"background": True, "running_seconds": 75},
            "foreground": 1,
            "background": 1,
            "oldest_wait_seconds": 75,
        }
        await worker.aclose()
        await asyncio.gather(first, *queued, return_exceptions=True)
        assert worker.diagnostics()["active"] is None

    asyncio.run(run())
