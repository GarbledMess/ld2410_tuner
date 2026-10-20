"""Production numerical jobs alongside the live HA HTTP loop; synthetic data only."""

import asyncio
import json
import statistics
import sys
import tempfile
import time
import types
import urllib.error
import urllib.request
from pathlib import Path

package = types.ModuleType("probe_tuner")
package.__path__ = [str(Path(__file__).resolve().parents[2] / "custom_components/ld2410_tuner")]
sys.modules[package.__name__] = package
from probe_tuner.history import commit, dayfile, daystore  # noqa: E402
from probe_tuner.worker import bridge, flushing, maintenance, startup, transactions  # noqa: E402


def health():
    started = time.monotonic()
    try:
        with urllib.request.urlopen("http://127.0.0.1:8123/api/", timeout=2) as response:
            assert response.status == 200
    except urllib.error.HTTPError as error:
        assert error.code == 401  # HA handled the request and enforced authentication.
    return time.monotonic() - started


async def main():
    with tempfile.TemporaryDirectory(prefix="tuner-production-probe-") as temporary:
        store = daystore.DayStore(Path(temporary))
        end = time.time()
        start = end - 6001
        store.append("old", dayfile.Change(end - 10 * 86400, {0: 25}))
        store.append("old", dayfile.End(end - 10 * 86400 + 6))
        for index in range(6000):
            base = 40 if index < 3000 else 5
            store.append(
                "probe",
                dayfile.Change(start + index, dict.fromkeys(range(4), base + index % 5)),
            )
        labels = [
            {"start": start, "end": start + 3000, "state": "present"},
            {"start": start + 3000, "end": end, "state": "not_present"},
        ]
        data = {"devices": {"probe": {"history_labels": labels}, "old": {}}, "room_snapshot": None}
        view = types.SimpleNamespace(
            data=data,
            _days=store,
            _history_until=end,
            _snapshot_history_start=start,
            _physical_history_start=start,
            _attribution_since=0,
        )
        runtime = types.SimpleNamespace(
            data=data,
            _days=store,
            _storage_lock=asyncio.Lock(),
            hass=types.SimpleNamespace(async_add_executor_job=asyncio.to_thread),
        )
        keys = ["g0_move", "g0_still", "g1_move", "g1_still"]
        parameters = {
            "device_id": "probe",
            "entities": {key: "number." + key for key in keys},
            "current": dict.fromkeys(keys, 20),
            "timing": {},
            "constraints": None,
        }
        stopped, latencies = asyncio.Event(), []

        async def heartbeat():
            while not stopped.is_set():
                latencies.append(await asyncio.to_thread(health))
                await asyncio.sleep(0.01)

        beat = asyncio.create_task(heartbeat())
        try:
            for _ in range(4):
                result = await bridge.calculate(
                    runtime, "learn", lambda: {"views": {"probe": view}, "parameters": parameters}
                )
                assert set(result["proposals"]) == set(keys)
            assert await flushing.flush(runtime) > 0
            store.append("probe", dayfile.End(end))
            assert await flushing.flush(runtime) > 0

            async def accept_storage(result, directory):
                prepared, changes, report, staged = await asyncio.to_thread(
                    maintenance.load, result, directory
                )
                assert changes and report["removed_automatic_seconds"] == 6
                assert await commit.publish(runtime, staged, lambda: True, lambda: None)
                return report

            await transactions.run(
                runtime,
                "maintenance",
                data,
                accept_storage,
                snapshot=lambda directory: {"clean": True, "target": None, "live": {}},
            )
            runtime._days = daystore.DayStore(store.root)
            await startup.initialize(runtime)
            observed = sum(
                len(runtime._days.day("probe", date).times) for date in runtime._days.days("probe")
            )
            assert observed == 6000
        finally:
            await bridge.stop(runtime)
            stopped.set()
            await beat
        assert not runtime._worker_service._jobs
        assert len(latencies) >= 5
        print(
            json.dumps(
                {
                    "production_learning_children": 4,
                    "production_storage_children": 4,
                    "storage_recovered_rows": observed,
                    "synthetic_rows": 6000,
                    "ha_http_responses": len(latencies),
                    "ha_http_p95_ms": round(statistics.quantiles(latencies, n=20)[18] * 1000, 3),
                    "ha_http_max_ms": round(max(latencies) * 1000, 3),
                    "workers_reaped": True,
                }
            ),
            flush=True,
        )


asyncio.run(main())
