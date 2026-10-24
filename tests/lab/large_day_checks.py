"""A dense but valid finished day through the actual protected chart child.

700,000 byte-valued rows inflate to about 18 MB before the child maps them: more than the
16 MiB per-file limit reading children had before 1.29.2, within the 128 MiB day allowance.
"""

import asyncio
import json
import resource
import sys
import tempfile
import time
import types
from array import array
from pathlib import Path

package = types.ModuleType("large_probe_tuner")
package.__path__ = [str(Path(__file__).resolve().parents[2] / "custom_components/ld2410_tuner")]
sys.modules[package.__name__] = package
from large_probe_tuner.const import HISTORY_KEYS  # noqa: E402
from large_probe_tuner.history import dayfile, daystore  # noqa: E402
from large_probe_tuner.worker import bridge, display  # noqa: E402

ROWS = 700_000
STEP = 0.12  # Seconds between rows: 84,000 s of one day.


async def main():
    with tempfile.TemporaryDirectory(prefix="tuner-large-day-") as temporary:
        store = daystore.DayStore(Path(temporary))
        start = (int(time.time()) // 86400 - 1) * 86400
        end = start + 86400
        times = array("d", (start + index * STEP for index in range(ROWS)))
        ends = array("d", (start + (index + 1) * STEP for index in range(ROWS)))
        columns = [bytes((index + gate) % 101 for index in range(ROWS)) for gate in range(18)]
        day = dayfile.Day(start, end, times, ends, columns, [])
        path = store._path("synthetic", daystore.date_of(start), ".day")
        path.parent.mkdir(parents=True)
        encoded = dayfile.encode_day(day)
        path.write_bytes(encoded)
        await asyncio.to_thread(store.open, end)
        data = {"devices": {"synthetic": {"history_labels": []}}}
        runtime = types.SimpleNamespace(
            data=data,
            _days=store,
            hass=types.SimpleNamespace(async_add_executor_job=asyncio.to_thread),
        )
        view = types.SimpleNamespace(data=data, _days=store, _history_until=end)
        parameters = {
            "device_ids": ["synthetic"],
            "keys": list(HISTORY_KEYS[:2]),
            "hours": 24,
            "max_points": 400,
            "end": end,
        }
        started = time.monotonic()
        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        try:
            result = await bridge.calculate(
                runtime, "charts", lambda: {"views": {"synthetic": view}, "parameters": parameters}
            )
            charts = display.unpack_charts(result, ["synthetic"])
            assert charts["errors"] == {}, charts["errors"]
            assert charts["charts"]["synthetic"]["series"], "no chart series"
        finally:
            await bridge.stop(runtime)
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
        print(
            json.dumps(
                {
                    "rows": ROWS,
                    "inflated_bytes": dayfile.HEADER.size + ROWS * 26,
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
