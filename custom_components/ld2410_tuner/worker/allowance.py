"""Worker limits sized from what Home Assistant can spare, and checked while a job runs.

The limits exist so that background work cannot hurt Home Assistant. They are not fixed
numbers, and they never come from how much a job wants:

- A child starts only while Home Assistant has room to spare: free memory (the host's, or
  Home Assistant's container's where that has a lower limit) above a reserve kept for
  Home Assistant plus a margin for the job. Otherwise the job waits for room.
- The child's memory (address-space) limit is a share of what is free beyond the
  reserve, between a floor and a ceiling. The floor is the old fixed limit: address
  space is reserved, not used, so the floor is what the largest jobs need to run at all,
  not what they take from Home Assistant.
- While the child runs, the supervisor watches Home Assistant itself (``Governor``). If
  Home Assistant's event loop falls behind, the child is paused until the loop has been
  responsive for a while. If memory runs short, the child is stopped and can run later.

CPU-time and file-size limits stay fixed. They only stop a runaway job: a job's CPU is
kept off Home Assistant by the lowest CPU and disk priority and by the pauses above.
Standard library only: the child imports this module before its limits are installed.
"""

import os
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from .diagnostics import _group, _memory, _pairs, _read

MIB = 1024 * 1024
CHILD = ("memory_mib", "cpu_seconds", "file_mib")
PREFIX = "budget="

RESERVE_MIB = 256  # Free memory always left to Home Assistant, at least...
RESERVE_SHARE = 0.10  # ...or this share of all memory, whichever is more.
START_MIB = 256  # Free beyond the reserve before a child may start.
MEMORY_SHARE = 0.5  # Of the rest, at most this much becomes the child's limit.
MEMORY_STALL = 10.0  # Percent of the last 10 s some task waited on memory: stop the child.
WAIT_POLL = 5.0  # Seconds between checks while a job waits for room.

GUARD_INTERVAL = 0.25  # Seconds between checks of a running child.
LAG_PAUSE = 0.1  # Event-loop lag (seconds) in every recent check: pause the child.
LAG_SAMPLES = 4  # So a pause needs about a second of sustained lag.
LAG_RESUME = 0.05  # Resume once the lag stays under this...
CALM_SECONDS = 5.0  # ...for this long.


@dataclass(frozen=True)
class Range:
    floor: int
    ceiling: int


@dataclass(frozen=True)
class Mode:
    memory_mib: Range  # Floor: what the largest job needs. Ceiling: never more.
    cpu_seconds: int
    file_mib: int
    wait_seconds: float | None  # Longest wait for room; None waits as long as it takes.
    run_after_wait: bool  # After the wait, run at the floor (True) or refuse (False).


MODES = {
    # Fits are optional and nobody waits on them: they wait for room as long as it takes.
    "fit": Mode(Range(1024, 2048), 900, 128, None, False),
    # Someone is looking at a chart: refuse rather than keep them waiting long.
    "display": Mode(Range(256, 512), 120, 128, 30.0, False),
    # Saves cannot be skipped: after a while they run at their floor, as they always did.
    # A storage child maps one inflated day (history.DAY_BYTES at most).
    "storage": Mode(Range(1024, 2048), 900, 192, 300.0, True),
}
STORAGE_OPERATIONS = frozenset({"startup", "maintenance", "flush"})
DISPLAY_OPERATIONS = frozenset({"charts", "recording_rows"})


def mode_for(operation):
    if operation in STORAGE_OPERATIONS:
        return "storage"
    return "display" if operation in DISPLAY_OPERATIONS else "fit"


def mode_of(args):
    """The child mode named by a worker's prepared arguments."""
    return args[2] if len(args) > 2 and args[2] in MODES else "fit"


def defaults(mode):
    """The floors: what a child gets when its parent names no budget."""
    child = MODES[mode]
    return {
        "memory_mib": child.memory_mib.floor,
        "cpu_seconds": child.cpu_seconds,
        "file_mib": child.file_mib,
    }


def argument(budget):
    """The child argument carrying a planned budget."""
    return PREFIX + ",".join(str(budget[kind]) for kind in CHILD)


def parse(token, mode):
    """A budget from the child argument; anything above the mode's maximums is refused."""
    if not token.startswith(PREFIX):
        raise ValueError("Expected a worker budget")
    values = token[len(PREFIX) :].split(",")
    if len(values) != len(CHILD) or not all(value.isdigit() for value in values):
        raise ValueError("Malformed worker budget")
    budget = dict(zip(CHILD, map(int, values), strict=True))
    child = MODES[mode]
    highest = {
        "memory_mib": child.memory_mib.ceiling,
        "cpu_seconds": child.cpu_seconds,
        "file_mib": child.file_mib,
    }
    if not all(0 < budget[kind] <= highest[kind] for kind in CHILD):
        raise ValueError("Worker budget outside its allowed range")
    return budget


@dataclass(frozen=True)
class Host:
    total_mib: int
    free_mib: int  # The host's available memory, or the container's headroom if lower.
    memory_stall: float  # Memory pressure (PSI "some" avg10), 0 where unavailable.


def _stall(text):
    """The "some avg10" percentage of a pressure file, 0 where absent or malformed."""
    some = next((line for line in text.splitlines() if line.startswith("some ")), "")
    fields = dict(field.split("=", 1) for field in some.split()[1:] if "=" in field)
    try:
        return float(fields.get("avg10", 0))
    except ValueError:
        return 0.0


def read_host(proc=Path("/proc"), cgroups=Path("/sys/fs/cgroup")):
    """What memory Home Assistant has now, or None where it cannot be read (not Linux)."""
    info = _pairs(proc / "meminfo")
    if "MemTotal" not in info or "MemAvailable" not in info:
        return None
    free = info["MemAvailable"] // 1024
    group = _memory(_group(proc, cgroups))
    limit, used = group.get("max_bytes"), group.get("current_bytes")
    if type(limit) is int and type(used) is int:
        free = min(free, max(0, limit - used) // MIB)
    return Host(info["MemTotal"] // 1024, free, _stall(_read(proc / "pressure/memory")))


def reserve_mib(host):
    return max(RESERVE_MIB, int(host.total_mib * RESERVE_SHARE))


def short_of_memory(host):
    """Home Assistant is short of memory: a running child must stop."""
    return host.free_mib < reserve_mib(host) or host.memory_stall >= MEMORY_STALL


def plan(mode, host):
    """The child's budget for what is free now, or None: not enough room to start yet."""
    budget = defaults(mode)
    if host is None:
        return budget  # Nothing to measure: the floors, as before.
    spare = host.free_mib - reserve_mib(host)
    if short_of_memory(host) or spare < START_MIB:
        return None
    allowed = MODES[mode].memory_mib
    budget["memory_mib"] = max(allowed.floor, min(allowed.ceiling, int(spare * MEMORY_SHARE)))
    return budget


class Policy:
    """Where a supervisor reads the host from; tests point it at fake files."""

    def __init__(self, *, proc=Path("/proc"), cgroups=Path("/sys/fs/cgroup")):
        self.proc, self.cgroups = proc, cgroups

    def host(self):
        return read_host(self.proc, self.cgroups) if os.name == "posix" else None


class Governor:
    """Decides, check by check, whether a running child should pause, resume or stop.

    ``max_pause`` bounds the pausing of one job: past it the child runs on, still at the
    lowest priority, so a Home Assistant that is always busy cannot stall saves forever.
    A save is never stopped (``stoppable``): it already waited for room, and an unsaved
    recording stays in Home Assistant's memory instead.
    """

    def __init__(self, max_pause, *, stoppable=True):
        self.max_pause = max_pause
        self.stoppable = stoppable
        self.lags = deque(maxlen=LAG_SAMPLES)
        self.paused_since = None
        self.calm_since = None
        self.paused_total = 0.0
        self.last_pause = 0.0

    def decide(self, lag, host, now):
        """``"stop"``, ``"pause"``, ``"resume"`` or None."""
        if self.stoppable and host is not None and short_of_memory(host):
            return "stop"
        self.lags.append(lag)
        if self.paused_since is None:
            return self._pause(now)
        return self._resume(lag, now)

    def _pause(self, now):
        sustained = len(self.lags) == LAG_SAMPLES and min(self.lags) > LAG_PAUSE
        if sustained and self.paused_total < self.max_pause:
            self.paused_since, self.calm_since = now, None
            return "pause"
        return None

    def _resume(self, lag, now):
        if lag >= LAG_RESUME:
            self.calm_since = None
        elif self.calm_since is None:
            self.calm_since = now
        calm = self.calm_since is not None and now - self.calm_since >= CALM_SECONDS
        if calm or self.paused_total + now - self.paused_since >= self.max_pause:
            self.last_pause = now - self.paused_since
            self.paused_total += self.last_pause
            self.paused_since = None
            self.lags.clear()
            return "resume"
        return None
