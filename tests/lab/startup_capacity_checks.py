"""Recover dense synthetic logs through protected workers alongside HA HTTP."""

import asyncio
import hashlib
import json
import resource
import statistics
import sys
import tempfile
import time
import types
import urllib.error
import urllib.request
from array import array
from pathlib import Path

package = types.ModuleType("startup_probe_tuner")
package.__path__ = [str(Path(__file__).resolve().parents[2] / "custom_components/ld2410_tuner")]
sys.modules[package.__name__] = package
from startup_probe_tuner.history import dayfile, daystore  # noqa: E402
from startup_probe_tuner.worker import bridge, flushing, startup  # noqa: E402


def health():
    started = time.monotonic()
    try:
        with urllib.request.urlopen("http://127.0.0.1:8123/api/", timeout=2) as response:
            assert response.status == 200
    except urllib.error.HTTPError as error:
        assert error.code == 401
    return time.monotonic() - started


def records(start, step, rows):
    for index in range(rows):
        at = start + index * step
        values = dict.fromkeys(range(18), 12.25) if index == 0 else {0: 12.25 + index % 2}
        yield dayfile.Change(at, values)
        if index % 5 == 0:
            yield dayfile.Guess(at, "present" if index % 10 else "not_present", 20, 1)


def fingerprint(builder):
    digest = hashlib.sha256()
    for column in (builder.times, builder.ends, *builder.columns):
        digest.update(column)
    digest.update(repr(builder.guesses).encode())
    return digest.hexdigest()


async def main():
    rows = 220000
    with tempfile.TemporaryDirectory(prefix="tuner-startup-capacity-") as temporary:
        store = daystore.DayStore(Path(temporary) / "history")
        today = daystore.date_of(time.time())
        start = daystore.bounds(today)[0]
        # All records belong to today's log, including when run shortly after midnight.
        step = max(0.001, (time.time() - start - 1) / rows)
        path = store._path("synthetic", today, ".log")
        path.parent.mkdir(parents=True)
        await asyncio.to_thread(
            path.write_bytes, dayfile.encode_records(records(start, step, rows), start)
        )
        runtime = types.SimpleNamespace(
            data={"devices": {"synthetic": {}}},
            _days=store,
            _storage_lock=asyncio.Lock(),
            hass=types.SimpleNamespace(async_add_executor_job=asyncio.to_thread),
        )
        stopped, latencies = asyncio.Event(), []

        async def heartbeat():
            while not stopped.is_set():
                latencies.append(await asyncio.to_thread(health))
                await asyncio.sleep(0.02)

        beat = asyncio.create_task(heartbeat())
        started = time.monotonic()
        try:
            await startup.initialize(runtime)
            live = store._live["synthetic"]
            assert len(live.builder.times) == rows
            assert len(live.builder.guesses) == rows // 5
            assert all(isinstance(column, array) for column in live.builder.columns)
            raw_bytes = rows * 88 + len(live.builder.guesses) * startup.RUN.size
            assert raw_bytes > 16 * 1024**2
            at = live.last + 0.01
            store.append("synthetic", dayfile.Change(at, {0: 42.25}))
            store.append("synthetic", dayfile.End(at + 0.01))
            assert await flushing.flush(runtime) > 0
            expected = await asyncio.to_thread(fingerprint, live.builder)
            runtime._days = daystore.DayStore(store.root)
            await startup.initialize(runtime)
            recovered = runtime._days._live["synthetic"].builder
            assert len(recovered.times) == rows + 1
            assert recovered.columns[0][-1] == 42.25
            assert await asyncio.to_thread(fingerprint, recovered) == expected
        finally:
            await bridge.stop(runtime)
            stopped.set()
            await beat
        assert len(latencies) >= 5
        assert not runtime._worker_service._jobs
        print(
            json.dumps(
                {
                    "startup_recoveries": 2,
                    "rows": rows,
                    "guess_runs": rows // 5,
                    "handoff_bytes": raw_bytes,
                    "history_preserved": True,
                    "wall_seconds": round(time.monotonic() - started, 3),
                    "child_peak_rss_kib": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
                    "ha_http_responses": len(latencies),
                    "ha_http_p95_ms": round(statistics.quantiles(latencies, n=20)[18] * 1000, 3),
                    "ha_http_max_ms": round(max(latencies) * 1000, 3),
                    "workers_reaped": True,
                }
            ),
            flush=True,
        )


if __name__ == "__main__":
    asyncio.run(main())
