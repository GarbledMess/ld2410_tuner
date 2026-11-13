"""What recording costs the HA host per flush, with the real worker; synthetic data only.

An evening's day logs for several busy radars, then a run of flushes as the 5 s sampler
schedules them: CPU of this process and its children, worker processes started, bytes copied
for the flushes, HA HTTP latency throughout, and every record checked back from the logs.
Prints one JSON report.
"""

import asyncio
import json
import resource
import shutil
import statistics
import sys
import tempfile
import time
import types
from pathlib import Path

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[2]
package = types.ModuleType("probe_tuner")
package.__path__ = [str(ROOT / "custom_components/ld2410_tuner")]
sys.modules[package.__name__] = package
from probe_tuner.history import dayfile, daystore  # noqa: E402
from probe_tuner.worker import flushing, transactions  # noqa: E402

RADARS = 6
EVENING_ROWS = 100_000  # Per radar: a busy radar records ~140k changes a day.
CYCLES = 60  # Five minutes of 5 s flushes.
PER_CYCLE = 10  # Changes per radar per 5 s on a busy radar.
PACE = 1.0  # Seconds between flushes, so HTTP latency is sampled alike.


HEARTBEAT = """
import json, resource, sys, time, urllib.error, urllib.request
samples = []
deadline = float(sys.argv[1])
while time.time() < deadline:
    started = time.monotonic()
    try:
        with urllib.request.urlopen("http://127.0.0.1:8123/api/", timeout=5) as response:
            assert response.status == 200
    except urllib.error.HTTPError as error:
        assert error.code == 401
    samples.append(time.monotonic() - started)
    time.sleep(0.05)
own = resource.getrusage(resource.RUSAGE_SELF)
print(json.dumps({"samples": samples, "cpu": own.ru_utime + own.ru_stime}))
"""


def cpu():
    own = resource.getrusage(resource.RUSAGE_SELF)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    return (
        own.ru_utime + own.ru_stime,
        children.ru_utime + children.ru_stime,
    )


def instrument(counters):
    """Count worker flushes and the bytes their private trees and backups copied."""
    run, copy, copy2 = transactions.run, transactions._copy, shutil.copy2

    async def counted_run(runtime, operation, *args, **kwargs):
        counters["workers"] += 1
        return await run(runtime, operation, *args, **kwargs)

    def counted_copy(source, *args, **kwargs):
        counters["copied"] += Path(source).stat().st_size
        return copy(source, *args, **kwargs)

    def counted_copy2(source, *args, **kwargs):
        counters["copied"] += Path(source).stat().st_size
        return copy2(source, *args, **kwargs)

    transactions.run = counted_run
    transactions._copy = counted_copy
    flushing.shutil.copy2 = counted_copy2


def records(start, spacing, count, offset):
    return [
        dayfile.Change(start + (offset + n) * spacing, {n % 18: 5 + (offset + n) % 90})
        for n in range(count)
    ]


async def main():
    counters = {"workers": 0, "copied": 0}
    with tempfile.TemporaryDirectory(prefix="tuner-recording-cost-") as temporary:
        store = daystore.DayStore(Path(temporary))
        now = time.time()
        day_start = daystore.bounds(daystore.date_of(now))[0]
        total = EVENING_ROWS + CYCLES * PER_CYCLE
        spacing = (now - day_start - 60) / total
        expected = {}
        for radar in range(RADARS):
            key = f"radar_{radar}"
            expected[key] = records(day_start + 1, spacing, EVENING_ROWS, 0)
            for record in expected[key]:
                store.append(key, record)
        runtime = types.SimpleNamespace(
            data={"devices": {f"radar_{radar}": {} for radar in range(RADARS)}},
            _days=store,
            _storage_lock=asyncio.Lock(),
            hass=types.SimpleNamespace(async_add_executor_job=asyncio.to_thread),
        )
        await flushing.flush(runtime)  # The evening so far, before measuring.
        log_bytes = sum(path.stat().st_size for path in Path(temporary).rglob("*.log"))
        instrument(counters)
        beat = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            HEARTBEAT,
            str(time.time() + CYCLES * PACE + 5),
            stdout=asyncio.subprocess.PIPE,
        )
        flush_seconds = []
        own_before, children_before = cpu()
        wall = time.monotonic()
        try:
            for cycle in range(CYCLES):
                offset = EVENING_ROWS + cycle * PER_CYCLE
                for key in expected:
                    added = records(day_start + 1, spacing, PER_CYCLE, offset)
                    expected[key] += added
                    for record in added:
                        store.append(key, record)
                started = time.monotonic()
                await flushing.flush(runtime)
                flush_seconds.append(time.monotonic() - started)
                await asyncio.sleep(PACE)  # Same cadence on every version.
        finally:
            heartbeat = json.loads((await beat.communicate())[0])
        wall = time.monotonic() - wall
        own_after, children_after = cpu()
        latencies = heartbeat["samples"]
        children_after -= heartbeat["cpu"]
        date = daystore.date_of(now)
        intact = all(
            dayfile.decode_log(store._path(key, date, ".log").read_bytes(), day_start) == wanted
            for key, wanted in expected.items()
        )
    report = {
        "code": str(ROOT),
        "radars": RADARS,
        "evening_log_mib": round(log_bytes / 2**20, 2),
        "flushes": CYCLES,
        "worker_processes": counters["workers"],
        "copied_mib": round(counters["copied"] / 2**20, 2),
        "cpu_seconds_integration": round(own_after - own_before, 3),
        "cpu_seconds_workers": round(children_after - children_before, 3),
        "flush_ms_median": round(statistics.median(flush_seconds) * 1000, 1),
        "flush_ms_max": round(max(flush_seconds) * 1000, 1),
        "wall_seconds": round(wall, 2),
        "http_requests": len(latencies),
        "http_ms_p95": round(statistics.quantiles(latencies, n=20)[18] * 1000, 2),
        "http_ms_max": round(max(latencies) * 1000, 2),
        "records_intact": intact,
    }
    print(json.dumps(report, indent=2))
    assert intact, "A recorded change was lost or duplicated"


asyncio.run(main())
