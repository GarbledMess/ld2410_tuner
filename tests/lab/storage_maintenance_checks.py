"""Synthetic large-history maintenance through the protected Linux storage child."""

import asyncio
import json
import random
import resource
import sys
import tempfile
import time
import types
from array import array
from pathlib import Path

source = Path(__file__).resolve().parents[2] / "custom_components/ld2410_tuner"
package = types.ModuleType("tuner_lab")
package.__path__ = [str(source)]
sys.modules[package.__name__] = package

from tuner_lab.history import dayfile, daystore, policy  # noqa: E402
from tuner_lab.worker import bridge, maintenance, retention, transactions  # noqa: E402


async def main():
    now, rows = time.time(), 300_000
    with tempfile.TemporaryDirectory(prefix="tuner-storage-probe-") as temporary:
        store = daystore.DayStore(Path(temporary) / "history")
        values = bytes(number % 101 for number in range(256))
        for offset in (*range(1, 30), 31):
            date = daystore.date_of(now - offset * policy.DAY)
            start, end = daystore.bounds(date)
            blob = random.Random(offset).randbytes(rows * 18).translate(values)
            day = dayfile.Day(
                start,
                end,
                array("d", (start + index * 0.25 for index in range(rows))),
                array("d", (start + (index + 1) * 0.25 for index in range(rows))),
                [blob[gate::18] for gate in range(18)],
                [],
            )
            store.replace("synthetic", date, day)
        source_bytes = store.size()
        store._cache.clear()
        store._cached = 0
        data = {
            "devices": {
                "synthetic": {
                    "history_labels": [
                        {"start": now - 30 * policy.DAY, "end": now, "state": "present"}
                    ]
                }
            },
            "storage_settings": {**policy.DEFAULTS, "max_mib": 512},
        }
        runtime = types.SimpleNamespace(
            _days=store,
            hass=types.SimpleNamespace(async_add_executor_job=asyncio.to_thread),
        )

        async def accept(result, directory):
            _prepared, changes, _report, _staged = maintenance.load(result, directory)
            assert len(changes) == 1, len(changes)
            assert isinstance(changes[0][2], daystore.StagedDay)
            seconds = result["counts"]["synthetic"]["seconds"]["g0_move"]["present"]
            assert sum(seconds) == 29 * rows * 0.25
            return {"profile": result["profile"], "changes": len(changes)}

        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        started = time.monotonic()
        try:
            result = await transactions.run(
                runtime,
                "maintenance",
                data,
                accept,
                snapshot=lambda _directory: {"clean": True, "target": None, "live": {}},
            )
            full_after = resource.getrusage(resource.RUSAGE_CHILDREN)
            full_observation = runtime._worker_service.last_observation
            full_wall = time.monotonic() - started
            batch_before = resource.getrusage(resource.RUSAGE_CHILDREN)
            batch_started = time.monotonic()
            current = data
            batch_peaks = []
            batch_profiles = []
            batch_count = 0

            async def accept_batch(output, directory):
                prepared, changes, report, _staged = maintenance.load(output, directory)
                assert len(changes) <= 7
                assert output["profile"]
                return prepared, report, output["profile"]

            for _ in range(31):
                batch_data = current
                prepared, report, profile = await transactions.run(
                    runtime,
                    "maintenance",
                    batch_data,
                    accept_batch,
                    snapshot=lambda _directory, batch_data=batch_data: {
                        "clean": True,
                        "target": None,
                        "live": {},
                        **retention.snapshot(store, batch_data, now),
                    },
                )
                batch_count += 1
                current = prepared
                batch_profiles.extend(profile)
                batch_peaks.append(runtime._worker_service.last_observation["sampled_peak_rss_kib"])
                if not report["pending"]:
                    break
            assert not report["pending"] and 1 <= batch_count <= 30, batch_count
            batch_after = resource.getrusage(resource.RUSAGE_CHILDREN)
            batch_result = {
                "batches": batch_count,
                "batch_wall_seconds": round(time.monotonic() - batch_started, 3),
                "batch_child_cpu_seconds": round(
                    batch_after.ru_utime
                    + batch_after.ru_stime
                    - batch_before.ru_utime
                    - batch_before.ru_stime,
                    3,
                ),
                "batch_sampled_peak_rss_kib": max(batch_peaks),
                "batch_stage_cpu_seconds": {
                    stage: round(
                        sum(
                            item["cpu_seconds"] for item in batch_profiles if item["stage"] == stage
                        ),
                        3,
                    )
                    for stage in {item["stage"] for item in batch_profiles}
                },
            }
        finally:
            await bridge.stop(runtime)
        print(
            json.dumps(
                {
                    "rows": rows * 30,
                    "days": 30,
                    "compressed_bytes": source_bytes,
                    "wall_seconds": round(full_wall, 3),
                    "child_cpu_seconds": round(
                        full_after.ru_utime
                        + full_after.ru_stime
                        - before.ru_utime
                        - before.ru_stime,
                        3,
                    ),
                    "child_peak_rss_kib": full_after.ru_maxrss,
                    "sampled_peak_rss_kib": full_observation["sampled_peak_rss_kib"],
                    "samples": full_observation["samples"],
                    **batch_result,
                    **result,
                }
            ),
            flush=True,
        )


asyncio.run(main())
