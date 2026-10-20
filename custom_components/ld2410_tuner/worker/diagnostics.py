"""Best-effort Linux worker evidence; bounded reads, no user paths or history values."""

import json
import os
import sys
import time
from pathlib import Path

PREFIX = "TUNER_WORKER "
STATUS = {"VmRSS": "rss_kib", "VmHWM": "peak_rss_kib", "VmSize": "virtual_kib"}


def _read(path):
    try:
        with path.open("r", encoding="ascii") as stream:
            return stream.read(16384)
    except (OSError, UnicodeError):
        return ""


def _pairs(path):
    result = {}
    for line in _read(path).splitlines():
        fields = line.replace(":", "").split()
        if len(fields) >= 2 and fields[1].isdigit():
            result[fields[0]] = int(fields[1])
    return result


def _group(proc, root):
    for line in _read(proc / "self/cgroup").splitlines():
        fields = line.split(":", 2)
        if len(fields) == 3 and fields[0:2] == ["0", ""]:
            relative = Path(fields[2].lstrip("/"))
            if ".." not in relative.parts:
                return root / relative
    return root


def _memory(group):
    result = {"events": _pairs(group / "memory.events")}
    for name in ("current", "peak", "max"):
        value = _read(group / ("memory." + name)).strip()
        if value.isdigit() or value == "max":
            result[name + "_bytes"] = int(value) if value.isdigit() else value
    result["events_available"] = bool(result["events"])
    return result


class Observation:
    """Only numeric resource evidence escapes this object; proc/cgroup paths stay private."""

    def __init__(self, *, proc=Path("/proc"), cgroups=Path("/sys/fs/cgroup")):
        self.proc, self.cgroups = proc, cgroups
        self.started = time.monotonic()
        self.last = {}
        self.samples = 0

    def start(self):
        self.group = self.cgroups
        self.before, self.host_before = {"events": {}}, None
        if sys.platform != "linux":
            return
        self.group = _group(self.proc, self.cgroups)
        self.before = _memory(self.group)
        self.host_before = _pairs(self.proc / "vmstat").get("oom_kill")

    def sample(self, pid):
        if sys.platform != "linux":
            return
        folder = self.proc / str(pid)
        status = _pairs(folder / "status")
        sample = {label: status[key] for key, label in STATUS.items() if key in status}
        fields = _read(folder / "stat").rpartition(") ")[2].split()
        try:
            sample["cpu_seconds"] = round(
                (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK"), 3
            )
        except (IndexError, ValueError, AttributeError, OSError):
            pass
        if sample:
            sample["elapsed_seconds"] = round(time.monotonic() - self.started, 3)
            self.last = sample
            self.samples += 1

    def report(self):
        after = _memory(self.group)
        old, new = self.before["events"], after["events"]
        delta = {key: value - old[key] for key, value in new.items() if key in old}
        host_after = _pairs(self.proc / "vmstat").get("oom_kill")
        host_delta = (
            None
            if self.host_before is None or host_after is None
            else host_after - self.host_before
        )
        available = _pairs(self.proc / "meminfo").get("MemAvailable")
        return {
            "elapsed_seconds": round(time.monotonic() - self.started, 3),
            "samples": self.samples,
            "last_sample": self.last,
            "cgroup_memory_before": self.before,
            "cgroup_memory_after": after,
            "cgroup_event_delta": delta,
            "host_oom_kill_delta": host_delta,
            "host_available_kib": available,
            "oom_scope": "shared cgroup/host counters; changes do not identify the killed process",
        }


def emit(phase, **details):
    """Trusted child milestones survive SIGKILL in the supervisor's capped stderr pipe."""
    import resource

    usage = resource.getrusage(resource.RUSAGE_SELF)
    record = {
        "phase": phase,
        "cpu_seconds": round(usage.ru_utime + usage.ru_stime, 3),
        "peak_rss_kib": usage.ru_maxrss,
        **details,
    }
    os.write(2, (PREFIX + json.dumps(record, separators=(",", ":")) + "\n").encode("ascii"))


def describe(request, request_bytes):
    """Report input scale, never device IDs, paths, labels, or energy values."""
    operation = request.get("operation")
    allowed = {
        "learn",
        "comparison",
        "room_assess",
        "room_fit",
        "room_compare",
        "seen_by",
        "references",
        "recount",
        "charts",
        "export_days",
        "startup",
        "maintenance",
        "flush",
    }
    views = request.get("views", {})
    entries = [entry for view in views.values() for entry in view.get("days", [])]
    files = {entry["path"]: int(entry["identity"][3]) for entry in entries if entry.get("identity")}
    return {
        "operation": operation if type(operation) is str and operation in allowed else "unknown",
        "views": len(views),
        "day_references": len(entries),
        "unique_files": len(files),
        "history_file_bytes": sum(files.values()),
        "request_bytes": request_bytes,
    }


def safely(function, *args):
    """Unavailable diagnostics must never prevent child execution or owned cleanup."""
    try:
        return function(*args)
    except Exception as error:
        return {"observation_error": type(error).__name__}
