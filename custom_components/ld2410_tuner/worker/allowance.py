"""Per-job worker limits that follow the host and the work, not fixed numbers.

Every limit has a floor and a ceiling. The floor is the old fixed limit, so a job never
gets less than it used to. The ceiling is the most a job can ever get. Between the two:

- a job gets a few times what recent jobs of the same operation used, so a home that
  grows gets more room as it grows;
- after a job stops at a limit, the next attempt gets twice that limit, and twice again
  after another stop, until the ceiling;
- address space never goes above half the host's RAM (or the floor, if that is more), so
  a large home on a small host cannot take what Home Assistant needs.

The child imports this module before its limits are installed: standard library only.
"""

import math
from dataclasses import dataclass
from pathlib import Path

MIB = 1024 * 1024
RECENT = 5  # Jobs remembered per operation.
MAX_BOOST = 8  # Three successive stops at the same limit.
# How far above the most a recent job used. Address space is mostly reserved up front by
# the numerical libraries, so it varies less than time does.
HEADROOM = {"memory_mib": 1.5, "cpu_seconds": 3.0, "wall_seconds": 3.0}
CHILD = ("memory_mib", "cpu_seconds", "file_mib")
PREFIX = "budget="


@dataclass(frozen=True)
class Range:
    floor: int
    ceiling: int


@dataclass(frozen=True)
class Child:
    memory_mib: Range
    cpu_seconds: Range
    file_mib: Range


# The child's mode (its third argument) picks its limits; the lane picks the wall deadline.
MODES = {
    # Fits are queued at the lowest priority and nobody waits on them.
    "fit": Child(Range(1024, 4096), Range(900, 7200), Range(128, 512)),
    # Display jobs are byte-bounded reads and may run beside a numerical child.
    "display": Child(Range(256, 1024), Range(120, 600), Range(128, 512)),
    # Storage children spill one inflated day (history.DAY_BYTES at most) before mapping it.
    "storage": Child(Range(1024, 4096), Range(900, 3600), Range(192, 768)),
}
WALL = {
    "fit": Range(3600, 14400),  # A busy host stretches a fit at the lowest priority.
    "short": Range(180, 900),  # Someone is waiting: a chart or a save.
}


def mode_of(args):
    """The child mode named by a worker's prepared arguments."""
    return args[2] if len(args) > 2 and args[2] in MODES else "fit"


def defaults(mode):
    """The floors: what a child gets when its parent names no budget."""
    child = MODES[mode]
    return {kind: getattr(child, kind).floor for kind in CHILD}


def argument(budget):
    """The child argument carrying a planned budget."""
    return PREFIX + ",".join(str(budget[kind]) for kind in CHILD)


def parse(token, mode):
    """A budget from the child argument; anything outside the mode's ceilings is refused."""
    if not token.startswith(PREFIX):
        raise ValueError("Expected a worker budget")
    values = token[len(PREFIX) :].split(",")
    if len(values) != len(CHILD) or not all(value.isdigit() for value in values):
        raise ValueError("Malformed worker budget")
    budget = dict(zip(CHILD, map(int, values), strict=True))
    child = MODES[mode]
    if not all(0 < budget[kind] <= getattr(child, kind).ceiling for kind in CHILD):
        raise ValueError("Worker budget outside its allowed range")
    return budget


def stopped_by(returncode, stderr):
    """Which limit ended a failed child, if a limit did. A plain kill is not counted: it may
    be the kernel's out-of-memory killer, which a larger allowance would only make likelier."""
    if returncode == -24 or b"CpuBudgetExceeded" in stderr:  # SIGXCPU
        return "cpu_seconds"
    if returncode == -25 or b"File too large" in stderr:  # SIGXFSZ, EFBIG
        return "file_mib"
    if b"MemoryError" in stderr:
        return "memory_mib"
    return None


def host_mib(meminfo=Path("/proc/meminfo")):
    """The host's total RAM in MiB, or None where it cannot be read."""
    try:
        with meminfo.open("r", encoding="ascii") as stream:
            text = stream.read(16384)
    except (OSError, UnicodeError):
        return None
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] == "MemTotal:" and fields[1].isdigit():
            return int(fields[1]) // 1024
    return None


class Allowances:
    """Learned allowances for one supervisor's jobs. Confined to its event loop, apart from
    ``child``, which reads the host's RAM once and so is first called off the loop."""

    def __init__(self, wall, *, meminfo=Path("/proc/meminfo")):
        self.wall_range = WALL[wall]
        self.meminfo = meminfo
        self._host = None
        self._used = {}
        self._boost = {}

    def wall(self, operation):
        return self._value(operation, "wall_seconds", self.wall_range)

    def child(self, operation, mode):
        ranges = MODES[mode]
        budget = {kind: self._value(operation, kind, getattr(ranges, kind)) for kind in CHILD}
        budget["memory_mib"] = min(budget["memory_mib"], self._memory_cap(ranges.memory_mib))
        return budget

    def succeeded(self, operation, used):
        """Remember what a finished job used and drop its boosts: they are now measured."""
        recent = self._used.setdefault(operation, {})
        for kind, value in used.items():
            if value is not None and math.isfinite(value) and value >= 0:
                history = recent.setdefault(kind, [])
                history.append(value)
                del history[:-RECENT]
        for key in [key for key in self._boost if key[0] == operation]:
            del self._boost[key]

    def stopped(self, operation, kind):
        """A job of this operation hit this limit: give the next one twice as much."""
        key = operation, kind
        self._boost[key] = min(MAX_BOOST, 2 * self._boost.get(key, 1))

    def _value(self, operation, kind, allowed):
        recent = self._used.get(operation, {}).get(kind, ())
        headroom = HEADROOM.get(kind, 1.0)
        wanted = max([allowed.floor, *(math.ceil(value * headroom) for value in recent)])
        return min(allowed.ceiling, wanted * self._boost.get((operation, kind), 1))

    def _memory_cap(self, allowed):
        if self._host is None:
            self._host = host_mib(self.meminfo) or 0
        return max(allowed.floor, min(allowed.ceiling, self._host // 2))
