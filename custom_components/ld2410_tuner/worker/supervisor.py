"""Bounded admission and owned subprocess cleanup, independent of Home Assistant.

The trusted script must install resource limits itself, before numerical imports.
This supervisor supplies no memory/CPU isolation by itself. Preparation is admitted
before it runs; queued requests must retain only small IDs, not history snapshots.
"""

import asyncio
import contextlib
import json
import math
import os
import signal
import subprocess  # nosec B404
import sys
import time
from pathlib import Path

from .diagnostics import Observation, safely

_POSIX = os.name == "posix"


class Busy(Exception):
    """The bounded pending queue is full."""


class Closed(Exception):
    """Shutdown or a cleanup failure prevents further admission."""


class OutputLimit(Exception):
    """A worker exceeded its stdout or stderr allowance."""


class CleanupFailed(Exception):
    """Ownership could not be released safely; do not start replacement work."""


class WorkerFailed(Exception):
    def __init__(self, returncode, stderr, *, diagnostics=None):
        detail = stderr[:2048].decode("utf-8", errors="replace").strip()
        self.diagnostics = {} if diagnostics is None else diagnostics
        reason = _exit_reason(returncode)
        evidence = "" if not self.diagnostics else "; diagnostics=" + json.dumps(self.diagnostics)
        super().__init__(f"Worker exited with status {returncode}{reason}: {detail}{evidence}")
        self.returncode, self.stderr = returncode, stderr


def _exit_reason(code):
    if code >= 0:
        return ""
    try:
        name = signal.Signals(-code).name
    except ValueError:
        # Linux production signals may be inspected by tools on a Windows host.
        name = {9: "SIGKILL", 24: "SIGXCPU", 25: "SIGXFSZ"}.get(-code, "unknown signal")
    detail = "; cause unconfirmed" if code == -9 else ""
    return f" ({name}{detail})"


async def _capture(stream, maximum):
    data = bytearray()
    while block := await stream.read(4096):
        if len(data) + len(block) > maximum:
            raise OutputLimit("Worker output exceeded its byte limit")
        data.extend(block)
    return bytes(data)


async def _drain(stream):
    while await stream.read(4096):
        pass


def _signal(process, *, force=False):
    try:
        if _POSIX:
            os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
        elif process.returncode is None:
            if force:
                process.kill()
            else:
                process.terminate()
    except ProcessLookupError:
        pass


async def _reap(process, grace):
    _signal(process)
    try:
        await asyncio.wait_for(process.wait(), grace)
    except TimeoutError:
        pass
    # The group can outlive its leader, even after that leader closed both pipes.
    _signal(process, force=True)
    try:
        await asyncio.wait_for(process.wait(), grace)
    except TimeoutError as error:
        raise CleanupFailed("Worker did not exit after termination and kill") from error


async def _cleanup(spawning, readers, grace):
    process = await spawning  # Shielded creation remains owned even after cancellation.
    for reader in readers:
        reader.cancel()
    await asyncio.gather(*readers, return_exceptions=True)
    drains = [asyncio.create_task(_drain(pipe)) for pipe in (process.stdout, process.stderr)]
    try:
        await _reap(process, grace)
        try:
            await asyncio.wait_for(asyncio.gather(*drains), grace)
        except TimeoutError as error:
            raise CleanupFailed("Worker output pipes remained open after exit") from error
    except CleanupFailed:
        raise
    except Exception as error:
        # Preserve the first failure; quarantine still prevents replacement work.
        with contextlib.suppress(Exception):
            _signal(process, force=True)
            await asyncio.wait_for(process.wait(), grace)
        raise CleanupFailed("Worker cleanup failed") from error
    finally:
        for drain in drains:
            drain.cancel()
        await asyncio.gather(*drains, return_exceptions=True)


async def _finish_cleanup(cleanup):
    cancelled = False
    while True:
        try:
            await asyncio.shield(cleanup)
            return cancelled
        except asyncio.CancelledError:
            if cleanup.cancelled():
                raise CleanupFailed("Owned cleanup was cancelled") from None
            cancelled = True


async def owned_preparation(awaitable):
    """Retain executor ownership after cancellation; invoke only after admission.

    A raw executor future cannot cancel its running thread. Preparers must wrap
    such futures here so cancellation cannot overlap copies from successive jobs.
    The underlying work must itself be bounded; this cannot interrupt a stuck OS call.
    """
    work = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.shield(work)
    finally:
        if await _finish_cleanup(work):
            raise asyncio.CancelledError


class Supervisor:
    """One active operation and a bounded pending queue, confined to one event loop.

    Cancelling a caller requests cancellation of its operation once. Cleanup retains
    admission after that caller returns; ``aclose`` waits for all ownership to end.
    A kernel-level stall during process creation cannot be forcibly interrupted here;
    its task retains the turn rather than allowing unowned replacement processes.
    """

    def __init__(
        self, script, *, max_pending=1, deadline=30, grace=1, stdout_bytes=65536, stderr_bytes=8192
    ):
        for value in (deadline, grace):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("Worker bounds must be finite and positive")
        for value in (stdout_bytes, stderr_bytes):
            if type(value) is not int or value <= 0:
                raise ValueError("Output limits must be positive integers")
        if type(max_pending) is not int or max_pending < 0:
            raise ValueError("max_pending must be a nonnegative integer")
        self.script = Path(script).resolve()
        self.deadline, self.grace = deadline, grace
        self.stdout_bytes, self.stderr_bytes = stdout_bytes, stderr_bytes
        self.max_pending = max_pending
        self._queue, self._waiting = [], set()
        self._active = None
        self._active_details = None
        self._clock = time.monotonic
        self.BACKGROUND_MAX_WAIT = 60
        self._jobs, self._cancelled = set(), set()
        self._closed, self._closing = False, None
        self._failure = None

    async def run(self, prepare, *, finish=None, consume=None):
        """Fail-fast admission; preparation waits for the chosen priority turn."""
        return await self._submit(prepare, finish, consume, deferred=False, background=False)

    async def run_when_available(self, prepare, *, finish=None, consume=None, background=False):
        """Queue only lightweight callables; callers must bound/deduplicate waiters.

        Foreground work takes the next turn. Background work remains deferred,
        preserving durability; a priority change never cancels an active child.
        """
        return await self._submit(prepare, finish, consume, deferred=True, background=background)

    async def _submit(self, prepare, finish, consume, *, deferred, background):
        if self._closed:
            raise Closed("Worker supervisor is closed")
        if not deferred and len(self._jobs) >= 1 + self.max_pending:
            raise Busy("Worker pending queue is full")
        work = asyncio.create_task(self._execute(prepare, finish, consume, background))
        (self._waiting if deferred else self._jobs).add(work)
        work.add_done_callback(self._finished)
        try:
            return await asyncio.shield(work)
        except asyncio.CancelledError:
            self._cancel(work)
            raise

    def _cancel(self, work):
        if work not in self._cancelled:
            self._cancelled.add(work)
            work.cancel()

    def _finished(self, work):
        self._jobs.discard(work)
        self._waiting.discard(work)
        self._cancelled.discard(work)
        if not work.cancelled():
            work.exception()

    def _dispatch(self):
        if self._active is not None:
            return
        self._queue[:] = [ticket for ticket in self._queue if not ticket[1].done()]
        if not self._queue:
            return
        ticket = self._next_ticket()
        self._queue.remove(ticket)
        self._active = ticket[1]
        self._active_details = ticket[0], self._clock()
        ticket[1].set_result(None)

    def _next_ticket(self):
        now = self._clock()
        aged = [
            ticket
            for ticket in self._queue
            if ticket[0] and now - ticket[2] >= self.BACKGROUND_MAX_WAIT
        ]
        if aged:
            return min(aged, key=lambda ticket: ticket[2])
        return next((ticket for ticket in self._queue if not ticket[0]), self._queue[0])

    def diagnostics(self):
        """Describe admission without retaining history inputs or exposing job arguments."""
        now = self._clock()
        queue = [ticket for ticket in self._queue if not ticket[1].done()]
        background = sum(ticket[0] for ticket in queue)
        active = None
        if self._active_details is not None:
            priority, started = self._active_details
            active = {"background": priority, "running_seconds": max(0, now - started)}
        return {
            "active": active,
            "foreground": len(queue) - background,
            "background": background,
            "oldest_wait_seconds": max((max(0, now - ticket[2]) for ticket in queue), default=0),
        }

    async def _execute(self, prepare, finish, consume, background):
        ticket = background, asyncio.get_running_loop().create_future(), self._clock()
        self._queue.append(ticket)
        self._dispatch()
        try:
            await ticket[1]
            if self._closed:
                raise Closed("Worker supervisor is closed")
            self._waiting.discard(asyncio.current_task())
            self._jobs.add(asyncio.current_task())
            try:
                return await self._owned(prepare, finish, consume)
            except CleanupFailed as error:
                self._closed = True
                self._failure = error
                raise
        finally:
            self._jobs.discard(asyncio.current_task())
            if ticket in self._queue:
                self._queue.remove(ticket)
            if self._active is ticket[1]:
                self._active = None
                self._active_details = None
            self._dispatch()

    async def _owned(self, prepare, finish, consume):
        try:
            async with asyncio.timeout(self.deadline):
                args = await prepare()
                if not isinstance(args, tuple) or not all(isinstance(a, str) for a in args):
                    raise ValueError("Prepared arguments must be a tuple of strings")
                raw = await self._process(args)
                return raw if consume is None else await consume(raw)
        finally:
            if finish is not None:
                cleanup = asyncio.create_task(_finalize(finish))
                if await _finish_cleanup(cleanup):
                    raise asyncio.CancelledError

    async def _process(self, args):
        observation = Observation()
        if sys.platform == "linux":
            await owned_preparation(asyncio.to_thread(safely, observation.start))
        else:
            observation.start()
        stopped, sampler = asyncio.Event(), None
        options = (
            {"start_new_session": True}
            if _POSIX
            else {"creationflags": subprocess.BELOW_NORMAL_PRIORITY_CLASS}
        )
        spawning = asyncio.create_task(
            asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                "-S",
                "-B",
                str(self.script),
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=8192,
                **options,
            )
        )
        readers = []
        try:
            process = await asyncio.shield(spawning)
            sampler = asyncio.create_task(_observe(observation, process.pid, stopped))
            readers = [
                asyncio.create_task(_capture(process.stdout, self.stdout_bytes)),
                asyncio.create_task(_capture(process.stderr, self.stderr_bytes)),
                asyncio.create_task(process.wait()),
            ]
            out, err, code = await asyncio.gather(*readers)
            if code:
                stopped.set()
                await owned_preparation(sampler)
                evidence = await owned_preparation(asyncio.to_thread(safely, observation.report))
                evidence.update(pid=process.pid, wall_deadline_seconds=self.deadline)
                raise WorkerFailed(code, err, diagnostics=evidence)
            return out
        finally:
            stopped.set()
            cleanup = asyncio.create_task(_observed_cleanup(spawning, readers, self.grace, sampler))
            if await _finish_cleanup(cleanup):
                raise asyncio.CancelledError

    async def aclose(self):
        """Stop accepting work and finish owned cleanup even if the shutdown caller cancels."""
        self._closed = True
        if self._closing is None:
            jobs = tuple(self._jobs | self._waiting)
            for work in jobs:
                self._cancel(work)
            self._closing = asyncio.ensure_future(asyncio.gather(*jobs, return_exceptions=True))
        await asyncio.shield(self._closing)
        if self._failure is not None:
            raise self._failure


async def _observe(observation, pid, stopped):
    while not stopped.is_set():
        await owned_preparation(asyncio.to_thread(safely, observation.sample, pid))
        try:
            await asyncio.wait_for(stopped.wait(), 0.5)
        except TimeoutError:
            pass


async def _observed_cleanup(spawning, readers, grace, sampler):
    try:
        await _cleanup(spawning, readers, grace)
    finally:
        if sampler is not None:
            await sampler


async def _finalize(finish):
    try:
        await finish()
    except BaseException as error:
        raise CleanupFailed("Worker input cleanup failed") from error
