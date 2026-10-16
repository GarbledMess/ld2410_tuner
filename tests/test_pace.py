"""Background work shares the CPU with Home Assistant instead of taking a whole core."""

import threading
import time
from unittest.mock import patch

import test_tuner  # noqa: F401
from tuner_under_test.history import pace


def work_in_thread(seconds_of_work, steps):
    """Burn ``seconds_of_work`` of CPU in ``steps`` slices, resting between them."""
    slept = []

    def run():
        with patch.object(pace.time, "sleep", side_effect=slept.append):
            for _ in range(steps):
                deadline = time.thread_time() + seconds_of_work / steps
                while time.thread_time() < deadline:
                    pass
                pace.rest()

    thread = threading.Thread(target=run)
    thread.start()
    thread.join()
    return slept


def test_background_threads_rest_as_long_as_they_worked():
    slept = work_in_thread(0.4, 8)
    assert slept and abs(sum(slept) - 0.4) < 0.15  # About half of one core at most.


def test_short_work_never_rests():
    assert work_in_thread(0.002, 4) == []


def test_the_event_loop_thread_never_sleeps():
    with patch.object(pace.time, "sleep", side_effect=AssertionError):
        deadline = time.thread_time() + 0.1
        while time.thread_time() < deadline:
            pass
        pace.rest()  # Main thread: returns at once.


def test_idle_time_between_jobs_is_not_counted_as_work():
    slept = []

    def run():
        with patch.object(pace.time, "sleep", side_effect=slept.append):
            pace.rest()
            threading.Event().wait(0.3)  # Idle: no CPU used, so nothing to rest for.
            pace.rest()

    thread = threading.Thread(target=run)
    thread.start()
    thread.join()
    assert slept == []
