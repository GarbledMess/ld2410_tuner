"""How long the real-shaped home's heaviest actions take, with its whole retained history.

    python tests/lab/real_shape_timing.py --output real-shape-timing.json [--quick]

The test suite Learns on radars with two days of history so it stays minutes long, and budgets
their cost; this report is what it costs on all sixty. On Linux every job runs in the production
child, under its real memory and CPU limits, so a job those limits refuse is reported as failed.
Elsewhere (a child refuses to run without its Linux limits) jobs run in this process.

It exits non-zero only if a job on a two-day radar is refused or fails. Timings, and how the
sixty-day jobs fared (a slow machine may meet the worker's CPU limit), are for reading and
comparing.
"""

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import real_home  # noqa: E402
import real_shape  # noqa: E402

REAL_CHILDREN = sys.platform == "linux"


def _cpu():
    """CPU seconds of this process and its finished children."""
    times = os.times()
    return times.user + times.system + times.children_user + times.children_system


async def _timed(found, name, action):
    wall, cpu = time.perf_counter(), _cpu()
    outcome = await action()
    found[name] = {
        "wall_seconds": round(time.perf_counter() - wall, 2),
        "cpu_seconds": round(_cpu() - cpu, 2),
        **(outcome or {}),
    }
    print(name, found[name], flush=True)


def _job(home, radar):
    async def run():
        _job_started, report = await home.learn(radar)
        return {"status": report["status"], "error": report.get("error")}

    return run


def _graph(home, radar):
    async def run():
        graph = await home.ws(
            "history_series_multi",
            device_id=radar,
            keys=list(real_shape.HISTORY_KEYS),
            hours=24,
            end=real_shape.TODAY - 5 * real_shape.DAY,
        )
        return {"status": "completed" if graph["series"] else "error"}

    return run


async def _measure(home, found, *, quick):
    await _timed(found, "start", home.start)
    await _timed(found, "graph_of_a_past_day", _graph(home, "office_1"))
    await _timed(found, "learn_radar_2_days", _job(home, "hall_1"))
    await _timed(found, "learn_room_2_radars_2_days", _job(home, "kitchen_1"))
    if not quick:
        await _timed(found, "learn_radar_60_days", _job(home, "office_1"))
        await _timed(found, "learn_again_radar_60_days", _job(home, "office_1"))
        await _timed(found, "learn_room_2_radars_60_days", _job(home, "living_room_1"))
    await _timed(found, "stop", home.stop)


def _run(home, found, quick):
    def scenario():
        return _measure(home, found, quick=quick)

    if REAL_CHILDREN:
        real_home.run_on_clock(home.clock, scenario)
    else:
        with real_home.worker_on_transport(home):
            real_home.run_on_clock(home.clock, scenario)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--quick", action="store_true", help="only the two-day Learns")
    options = parser.parse_args(argv)
    found = {"real_worker_children": REAL_CHILDREN}
    if REAL_CHILDREN:
        real_shape.set_now(int(time.time()))
    with tempfile.TemporaryDirectory(prefix="real-shape-timing-") as folder:
        started = time.perf_counter()
        template = real_shape.build(Path(folder) / "template")
        found["build_seconds"] = round(time.perf_counter() - started, 2)
        home = real_home.open_home(template, Path(folder) / "config")
        _run(home, found, options.quick)
    Path(options.output).parent.mkdir(parents=True, exist_ok=True)
    Path(options.output).write_text(json.dumps(found, indent=2) + "\n", encoding="utf8")
    failed = [
        name
        for name, item in found.items()
        if "60_days" not in name
        and isinstance(item, dict)
        and item.get("status") not in (None, "completed")
    ]
    if failed:
        print("Failed:", failed, file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
