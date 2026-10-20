"""Synthetic full-day legacy history through the actual protected learning child."""

import asyncio
import json
import resource
import sys
import tempfile
import time
import types
from array import array
from pathlib import Path

package = types.ModuleType("dense_probe_tuner")
package.__path__ = [str(Path(__file__).resolve().parents[2] / "custom_components/ld2410_tuner")]
sys.modules[package.__name__] = package
from dense_probe_tuner.const import HISTORY_KEYS  # noqa: E402
from dense_probe_tuner.history import dayfile, daystore  # noqa: E402
from dense_probe_tuner.worker import bridge  # noqa: E402


async def main():
    rows = 86400
    with tempfile.TemporaryDirectory(prefix="tuner-dense-probe-") as temporary:
        store = daystore.DayStore(Path(temporary))
        start = (int(time.time()) // rows - 1) * rows
        end = start + rows
        day = dayfile.Day(
            start,
            end,
            array("d", range(start, end)),
            array("d", range(start + 1, end + 1)),
            [
                array(
                    "f", (40.25 + i % 13 if i < rows // 2 else 5.25 + i % 13 for i in range(rows))
                )
                for _ in range(18)
            ],
            [],
        )
        date = daystore.date_of(start)
        path = store._path("synthetic", date, ".day")
        path.parent.mkdir(parents=True)
        encoded = dayfile.encode_day(day)
        path.write_bytes(encoded)
        await asyncio.to_thread(store.open, end)
        data = {
            "devices": {
                "synthetic": {
                    "history_labels": [
                        {"start": start, "end": start + rows // 2, "state": "present"},
                        {"start": start + rows // 2, "end": end, "state": "not_present"},
                    ]
                }
            }
        }
        runtime = types.SimpleNamespace(
            data=data,
            _days=store,
            hass=types.SimpleNamespace(async_add_executor_job=asyncio.to_thread),
        )
        view = types.SimpleNamespace(data=data, _days=store, _history_until=end)
        parameters = {
            "device_id": "synthetic",
            "entities": {key: "number." + key for key in HISTORY_KEYS},
            "current": dict.fromkeys(HISTORY_KEYS, 20),
            "timing": {},
            "constraints": None,
        }
        started = time.monotonic()
        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        try:
            result = await bridge.calculate(
                runtime, "learn", lambda: {"views": {"synthetic": view}, "parameters": parameters}
            )
            assert set(result["proposals"]) == set(HISTORY_KEYS)
        finally:
            await bridge.stop(runtime)
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
        print(
            json.dumps(
                {
                    "rows": rows,
                    "legacy_float_gates": 18,
                    "wire_raw_bytes": rows * 80 + dayfile.HEADER.size,
                    "compressed_bytes": len(encoded),
                    "wall_seconds": round(time.monotonic() - started, 3),
                    "child_cpu_seconds": round(
                        after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime, 3
                    ),
                    "child_peak_rss_kib": after.ru_maxrss,
                    "mandatory_limits": True,
                }
            ),
            flush=True,
        )


asyncio.run(main())
