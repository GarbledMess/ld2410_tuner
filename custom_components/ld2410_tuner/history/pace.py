"""Background work shares the CPU with Home Assistant instead of taking a whole core.

Heavy jobs (history cleanup, label recounts, reference rebuilds, learning) run in executor
threads, but pure-Python work there still competes with Home Assistant's event loop for the
interpreter. Their loops call ``rest`` between units of work: once a thread has spent
``SLICE`` seconds of its own CPU time since it last rested, it sleeps as long again, so a job
uses at most ``SHARE`` of one core and leaves the rest to Home Assistant. Idle time is not
work (thread CPU time, not wall time). The event loop's thread never sleeps here.
"""

import threading
import time

SHARE = 0.5  # Of one core, at most, for a background job.
SLICE = 0.05  # Seconds of CPU between rests.
_state = threading.local()


def rest():
    """Call between units of background work (a day, a radar, a search step)."""
    if threading.current_thread() is threading.main_thread():
        return
    used = time.thread_time()
    since = getattr(_state, "since", None)
    if since is None:
        _state.since = used
        return
    worked = used - since
    if worked >= SLICE:
        time.sleep(worked * (1 - SHARE) / SHARE)
        _state.since = time.thread_time()
