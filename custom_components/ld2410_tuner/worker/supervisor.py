"""Bounded admission and owned subprocess cleanup, independent of Home Assistant.

The trusted script must install resource limits itself, before numerical imports.
This supervisor supplies no memory/CPU isolation by itself. Preparation is admitted
before it runs; queued requests must retain only small IDs, not history snapshots.
"""

import asyncio
import math
import os
import signal
import subprocess
import sys
from pathlib import Path

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
    def __init__(self, returncode, stderr):
        detail = stderr[:2048].decode("utf-8", errors="replace").strip()
        super().__init__(f"Worker exited with status {returncode}: {detail}")
        self.returncode, self.stderr = returncode, stderr


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
        try:
            _signal(process, force=True)
            await asyncio.wait_for(process.wait(), grace)
        except Exception:
            pass  # Preserve the first failure; quarantine still prevents replacement work.
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
        self._turn = asyncio.Lock()
        self._jobs, self._cancelled = set(), set()
        self._closed, self._closing = False, None
        self._failure = None

    async def run(self, prepare, *, finish=None, consume=None):
        """Admit async ``prepare`` returning string arguments; retain any executor work.

        Preparers using an executor must await it through ``owned_preparation``.
        A preparation deadline cancels the request, but ownership remains until
        bounded preparation and process cleanup actually finish.
        """
        if self._closed:
            raise Closed("Worker supervisor is closed")
        if len(self._jobs) >= 1 + self.max_pending:
            raise Busy("Worker pending queue is full")
        work = asyncio.create_task(self._execute(prepare, finish, consume))
        self._jobs.add(work)
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
        self._cancelled.discard(work)
        if not work.cancelled():
            work.exception()

    async def _execute(self, prepare, finish, consume):
        async with self._turn:
            if self._closed:
                raise Closed("Worker supervisor is closed")
            try:
                return await self._owned(prepare, finish, consume)
            except CleanupFailed as error:
                self._closed = True
                self._failure = error
                raise

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
            readers = [
                asyncio.create_task(_capture(process.stdout, self.stdout_bytes)),
                asyncio.create_task(_capture(process.stderr, self.stderr_bytes)),
                asyncio.create_task(process.wait()),
            ]
            out, err, code = await asyncio.gather(*readers)
            if code:
                raise WorkerFailed(code, err)
            return out
        finally:
            cleanup = asyncio.create_task(_cleanup(spawning, readers, self.grace))
            if await _finish_cleanup(cleanup):
                raise asyncio.CancelledError

    async def aclose(self):
        """Stop accepting work and finish owned cleanup even if the shutdown caller cancels."""
        self._closed = True
        if self._closing is None:
            jobs = tuple(self._jobs)
            for work in jobs:
                self._cancel(work)
            self._closing = asyncio.ensure_future(asyncio.gather(*jobs, return_exceptions=True))
        await asyncio.shield(self._closing)
        if self._failure is not None:
            raise self._failure


async def _finalize(finish):
    try:
        await finish()
    except BaseException as error:
        raise CleanupFailed("Worker input cleanup failed") from error
