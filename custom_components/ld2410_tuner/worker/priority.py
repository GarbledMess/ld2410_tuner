"""Task-local worker priority; scheduling changes never cancel active work."""

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps

_mode = ContextVar("tuner_worker_priority", default="normal")


def is_background(operation=None):
    mode = _mode.get()
    return mode == "background" or (mode == "normal" and operation in {"seen_by", "references"})


@contextmanager
def interactive():
    token = _mode.set("interactive")
    try:
        yield
    finally:
        _mode.reset(token)


def background(function):
    @wraps(function)
    async def run(*args, **kwargs):
        token = _mode.set("background")
        try:
            return await function(*args, **kwargs)
        finally:
            _mode.reset(token)

    return run
