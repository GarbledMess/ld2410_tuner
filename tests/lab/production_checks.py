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
from probe_tuner.const import HISTORY_KEYS  # noqa: E402
from probe_tuner.history import commit, dayfile, daystore  # noqa: E402
from probe_tuner.worker import (  # noqa: E402
    bridge,
    display,
    flushing,
    maintenance,
    startup,
    transactions,
)


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
        row_count = 150000  # Match dense delivered-event days in the supplied recordings.
        for index in range(row_count):
            base = 40 if index < row_count // 2 else 5
            store.append(
                "probe",
                dayfile.Change(start + index * 0.04, dict.fromkeys(range(18), base + index % 5)),
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

            chart = await bridge.calculate(
                runtime,
                "charts",
                lambda: {
                    "views": {"probe": view},
                    "parameters": {
                        "device_id": "probe",
                        "keys": list(HISTORY_KEYS),
                        "hours": 2,
                        "max_points": 400,
                        "end": end,
                    },
                },
            )
            chart = await asyncio.to_thread(display.unpack_chart, chart)
            assert set(chart["series"]) == set(HISTORY_KEYS)
            assert all(len(series["points"]) >= 200 for series in chart["series"].values())

            # A room graph must not wait behind learning: it has its own display child.
            learning = asyncio.create_task(
                bridge.calculate(
                    runtime, "learn", lambda: {"views": {"probe": view}, "parameters": parameters}
                )
            )
            await asyncio.sleep(0.5)
            chart_started = time.monotonic()
            summary = await bridge.calculate(
                runtime,
                "charts",
                lambda: {
                    "views": {"probe": view},
                    "parameters": {
                        "device_id": "probe",
                        "keys": list(HISTORY_KEYS),
                        "hours": 2,
                        "max_points": 400,
                        "end": end,
                        "summary": True,
                    },
                },
            )
            summary_seconds = time.monotonic() - chart_started
            summary_beside_learning = not learning.done()
            await learning
            summary = await asyncio.to_thread(display.unpack_chart, summary)
            # Every gate has the same value at each instant in this fixture,
            # so their simultaneous maximum is exactly one gate's native curve.
            assert summary["points"] == chart["series"][HISTORY_KEYS[0]]["points"]
            assert (summary["start"], summary["end"]) == (chart["start"], chart["end"])

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
            assert observed == row_count
        finally:
            await bridge.stop(runtime)
            stopped.set()
            await beat
        assert not runtime._worker_service._jobs
        assert not runtime._display_service._jobs
        assert len(latencies) >= 5
        print(
            json.dumps(
                {
                    "production_learning_children": 4,
                    "protected_chart_gates": len(chart["series"]),
                    "room_summary_points": len(summary["points"]),
                    "full_chart_json_bytes": len(json.dumps(chart, separators=(",", ":"))),
                    "room_summary_json_bytes": len(json.dumps(summary, separators=(",", ":"))),
                    "room_summary_simultaneous_truth": True,
                    "room_summary_seconds": round(summary_seconds, 3),
                    "room_summary_beside_learning": summary_beside_learning,
                    "protected_chart_points": sum(
                        len(s["points"]) for s in chart["series"].values()
                    ),
                    "production_storage_children": 4,
                    "storage_recovered_rows": observed,
                    "synthetic_rows": row_count,
                    "ha_http_responses": len(latencies),
                    "ha_http_p95_ms": round(statistics.quantiles(latencies, n=20)[18] * 1000, 3),
                    "ha_http_max_ms": round(max(latencies) * 1000, 3),
                    "workers_reaped": True,
                }
            ),
            flush=True,
        )


asyncio.run(main())
