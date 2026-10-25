"""Report stalled durability without adding another polling loop."""

import logging
import sys
import threading
import time
import traceback

from ..worker import flushing

# A save can wait behind one long worker job (a room Learn takes minutes); only well beyond
# that is it stuck.
HISTORY_STALL_SECONDS = 900
DIAGNOSTIC_BYTES = 19 * 1024
_LOGGER = logging.getLogger(__name__)


def check(runtime):
    now = time.time()
    if runtime._days is None or not flushing._pending(runtime._days):
        completed(runtime, now=now)
        return
    waiting = getattr(runtime, "_history_waiting_since", None)
    if waiting is None:
        runtime._history_waiting_since = now
    elif now - waiting >= HISTORY_STALL_SECONDS and not getattr(
        runtime, "_history_stall_warned", False
    ):
        runtime._history_stall_warned = True
        _LOGGER.warning(
            "Recorded history has not been saved for %s min; recent readings are only in memory\n%s",
            int((now - waiting) // 60),
            diagnostics(runtime),
        )


def completed(runtime, *, now=None):
    """A successful flush starts a new grace period for concurrently arriving records."""
    now = time.time() if now is None else now
    waiting = getattr(runtime, "_history_waiting_since", None)
    if getattr(runtime, "_history_stall_warned", False):
        _LOGGER.warning("Recorded history saving resumed after %s min", int((now - waiting) // 60))
    runtime._history_stall_warned = False
    runtime._history_saved_at = now
    runtime._history_waiting_since = (
        now if runtime._days is not None and flushing._pending(runtime._days) else None
    )


def _bounded(value, limit):
    return value.encode("utf-8", errors="replace")[:limit].decode("utf-8", errors="ignore")


def _task_details(task):
    if task is None:
        return "flush task: absent"
    state = "cancelled" if task.cancelled() else "done" if task.done() else "pending"
    frames = task.get_stack(limit=16)
    stack = "".join(_frame(frame) for frame in frames)
    return _bounded(f"flush task: {state} {task.get_name()}\n{stack}", 4096)


def _frame(frame):
    return _bounded("".join(traceback.format_stack(frame, limit=1)), 1024)


def _threads():
    names = {thread.ident: thread.name for thread in threading.enumerate()}
    frames = sys._current_frames()
    budget = 12000 // max(1, len(frames))
    parts = []
    for ident, frame in frames.items():
        stack = []
        while frame is not None and len(stack) < 16:
            stack.append(_frame(frame))
            frame = frame.f_back
        parts.append(
            _bounded(f"thread {names.get(ident, 'unknown')} ({ident})\n" + "".join(stack), budget)
        )
    return "\n".join(parts)


def diagnostics(runtime):
    services = []
    for name in ("_worker_service", "_final_storage_service"):
        service = getattr(runtime, name, None)
        state = None if service is None else service.diagnostics()
        services.append(f"worker {name}: {state}")
    return _bounded(
        "\n".join(services + [_task_details(runtime._flush_task), _threads()]),
        DIAGNOSTIC_BYTES,
    )
