"""Required Linux protection for a disposable child, before numerical imports.

Call only in a fresh ``-I -S`` child. Failure is fatal for that child: some
irreversible controls may already be installed. Never call in Home Assistant.
Budgets are explicit; this module does not select production job allowances.
"""

import ctypes
import os
import signal
import sys
from dataclasses import dataclass
from pathlib import Path


class IsolationUnavailable(RuntimeError):
    """The child cannot establish every required protection."""


@dataclass(frozen=True)
class Budget:
    memory_bytes: int
    cpu_seconds: int
    file_bytes: int
    open_files: int = 64

    def __post_init__(self):
        for value in (self.memory_bytes, self.cpu_seconds, self.file_bytes, self.open_files):
            if type(value) is not int or not 0 < value < 2**63:
                raise ValueError("Child budgets must be positive signed 64-bit integers")


def _parent(expected):
    if os.getppid() != expected:
        raise IsolationUnavailable("Child owner disappeared during protection setup")


def _process(expected_parent):
    _parent(expected_parent)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0) != 0:  # PR_SET_PDEATHSIG
        raise IsolationUnavailable("Parent-death protection is unavailable")
    _parent(expected_parent)
    os.nice(max(0, 19 - os.getpriority(os.PRIO_PROCESS, 0)))
    if os.getpriority(os.PRIO_PROCESS, 0) != 19:
        raise IsolationUnavailable("Low child scheduling priority was not installed")
    score = Path("/proc/self/oom_score_adj")
    desired = max(500, int(score.read_text()))
    score.write_text(str(desired))
    actual = int(score.read_text())
    if actual < desired:
        raise IsolationUnavailable("Child retained protected OOM priority")
    return actual


def _limit(resource, kind, requested):
    existing = resource.getrlimit(kind)
    ceiling = min([requested, *(value for value in existing if value != resource.RLIM_INFINITY)])
    resource.setrlimit(kind, (ceiling, ceiling))
    if resource.getrlimit(kind) != (ceiling, ceiling):
        raise IsolationUnavailable("Kernel did not retain the required hard limit")
    return ceiling


def install(budget: Budget, *, expected_parent: int):
    """Install mandatory controls or refuse execution; never fall back to threads.

    Resource limits belong to this process; the supervisor owns its process group
    and wall deadline. Address-space limits do not establish a group RSS ceiling.
    Existing stricter soft/hard limits and higher OOM adjustments are preserved.
    """
    if type(expected_parent) is not int or expected_parent <= 1:
        raise ValueError("A live owning parent PID is required")
    if sys.platform != "linux" or not sys.flags.isolated or not sys.flags.no_site:
        raise IsolationUnavailable(
            "Protection requires an isolated Linux child without site imports"
        )
    try:
        import resource

        oom = _process(expected_parent)
        controls = {
            "memory_bytes": (resource.RLIMIT_AS, budget.memory_bytes),
            "cpu_seconds": (resource.RLIMIT_CPU, budget.cpu_seconds),
            "file_bytes": (resource.RLIMIT_FSIZE, budget.file_bytes),
            "core_bytes": (resource.RLIMIT_CORE, 0),
            "open_files": (resource.RLIMIT_NOFILE, budget.open_files),
        }
        installed = {name: _limit(resource, *values) for name, values in controls.items()}
    except (OSError, ValueError, ImportError, AttributeError) as error:
        raise IsolationUnavailable("Required child protection could not be installed") from error
    return {"nice": 19, "oom_score_adj": oom, "parent_pid": expected_parent, **installed}
