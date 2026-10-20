"""Cooperative pacing for synchronous compatibility and offline calculation helpers.

Production numerical and storage jobs use mandatory limited subprocesses. This
helper remains for synchronous callers and offline tools; it is not their process
isolation boundary. ``run`` admits one paced job before borrowing an HA executor
thread. ``background`` uses a single owned thread, so lowering its priority never
changes an HA thread reused for unrelated work. ``rest`` yields after a CPU slice;
this cooperative target supplies no hard CPU or memory guarantee.

``stop`` abandons paced jobs at their next checkpoint. Process shutdown and
resource limits are owned separately by the worker supervisor.
"""

import asyncio
import functools
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from weakref import WeakKeyDictionary

SHARE = 0.2  # Target CPU share for all expensive integration work together.
SLICE = 0.005  # Seconds of CPU between rests (coarse CPU clocks can overrun this).
_state = threading.local()
_worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ld2410-background")
_generation = [0]  # Bumped by ``stop``: jobs of an earlier generation are abandoned.
_admission = WeakKeyDictionary()


class Stopped(Exception):
    """The integration is stopping: background work is abandoned."""


async def run(hass, fn, *args):
    """Wait for our turn without occupying Home Assistant's shared executor threads."""
    generation = _generation[0]
    loop = asyncio.get_running_loop()
    turn = _admission.setdefault(loop, asyncio.Lock())
    await turn.acquire()
    try:
        if generation != _generation[0]:
            raise Stopped
        work = asyncio.ensure_future(hass.async_add_executor_job(_admitted(fn, generation), *args))
    except BaseException:
        turn.release()
        raise
    work.add_done_callback(functools.partial(_finished, turn))
    # Cancelling the caller cannot stop a running executor thread. Keep its turn until
    # the real work finishes; otherwise abandoned workers can fill HA's shared pool.
    return await asyncio.shield(work)


def _finished(turn, work):
    turn.release()
    if not work.cancelled():
        work.exception()  # Retrieve failures even when the caller has gone away.


def _admitted(fn, generation):
    @functools.wraps(fn)
    def execute(*args):
        return _worker.submit(_perform, fn, args, {}, generation).result()

    return execute


def background(fn):
    """Make ``fn`` a low-priority background job (see the module docstring)."""
    if getattr(fn, "_background", False):
        return fn

    @functools.wraps(fn)
    def governed(*args, **kwargs):
        if threading.current_thread() is threading.main_thread() or _active():
            return fn(*args, **kwargs)  # The event loop, or a job inside a job.
        generation = _generation[0]
        return _worker.submit(_perform, fn, args, kwargs, generation).result()

    governed._background = True
    return governed


def _perform(fn, args, kwargs, generation):
    if generation != _generation[0]:
        raise Stopped
    previous = _lower()
    _state.background, _state.generation = True, generation
    _state.since = time.thread_time()
    try:
        return fn(*args, **kwargs)
    finally:
        _state.background = False
        _restore(previous)


def _active():
    return getattr(_state, "background", False)


def rest():
    """Call between units of background work (a day, a radar, a search step)."""
    if not _active():
        return
    if _state.generation != _generation[0]:
        raise Stopped
    worked = time.thread_time() - _state.since
    if worked >= SLICE and SHARE < 1:
        time.sleep(worked * (1 - SHARE) / SHARE)
        _state.since = time.thread_time()


def stop():
    """Unload: abandon the background work waiting or running now, at its next ``rest``."""
    _generation[0] += 1


def _lower():
    """Lowest scheduling priority for this thread (Linux: a thread's nice value is its own)."""
    try:
        thread = threading.get_native_id()
        previous = os.getpriority(os.PRIO_PROCESS, thread)
        os.setpriority(os.PRIO_PROCESS, thread, 19)
        return previous
    except (AttributeError, OSError):
        return None


def _restore(previous):
    if previous is None:
        return
    try:
        os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), previous)
    except OSError:
        pass  # Raising a priority back may need privileges: the thread stays lower.
