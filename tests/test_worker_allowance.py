"""Worker limits follow the host and recent jobs between fixed floors and ceilings."""

import asyncio
import sys

import pytest
import test_tuner  # noqa: F401
from tuner_under_test.worker import allowance, entrypoint, supervisor


def meminfo(tmp_path, total_kib):
    path = tmp_path / "meminfo"
    path.write_text(f"MemTotal:       {total_kib} kB\nMemAvailable:   1 kB\n")
    return path


def test_a_new_lane_starts_at_the_old_fixed_limits(tmp_path):
    plan = allowance.Allowances("fit", meminfo=meminfo(tmp_path, 8 * 1024 * 1024))
    assert plan.wall("learn") == 3600
    assert plan.child("learn", "fit") == {"memory_mib": 1024, "cpu_seconds": 900, "file_mib": 128}
    assert plan.child("flush", "storage")["file_mib"] == 192
    assert plan.child("charts", "display")["memory_mib"] == 256
    assert allowance.Allowances("short").wall("charts") == 180


def test_allowances_grow_with_what_recent_jobs_used(tmp_path):
    plan = allowance.Allowances("fit", meminfo=meminfo(tmp_path, 8 * 1024 * 1024))
    plan.succeeded("learn", {"cpu_seconds": 500, "wall_seconds": 2000, "memory_mib": 1000})
    plan.succeeded("learn", {"cpu_seconds": 100, "wall_seconds": None})
    assert plan.child("learn", "fit")["cpu_seconds"] == 1500
    assert plan.child("learn", "fit")["memory_mib"] == 1500
    assert plan.wall("learn") == 6000
    assert plan.child("comparison", "fit")["cpu_seconds"] == 900  # Per operation.
    for _ in range(allowance.RECENT):
        plan.succeeded("learn", {"cpu_seconds": 10})
    assert plan.child("learn", "fit")["cpu_seconds"] == 900  # Never below the floor.


def test_a_stop_at_a_limit_doubles_the_next_attempt_up_to_the_ceiling(tmp_path):
    plan = allowance.Allowances("fit", meminfo=meminfo(tmp_path, 64 * 1024 * 1024))
    plan.stopped("learn", "cpu_seconds")
    assert plan.child("learn", "fit")["cpu_seconds"] == 1800
    for _ in range(5):
        plan.stopped("learn", "cpu_seconds")
    assert plan.child("learn", "fit")["cpu_seconds"] == 7200
    plan.stopped("learn", "wall_seconds")
    assert plan.wall("learn") == 7200
    plan.succeeded("learn", {"cpu_seconds": 1000})
    assert plan.child("learn", "fit")["cpu_seconds"] == 3000
    assert plan.wall("learn") == 3600


def test_memory_never_takes_more_than_half_the_host(tmp_path):
    small = allowance.Allowances("fit", meminfo=meminfo(tmp_path, 3 * 1024 * 1024))
    small.stopped("learn", "memory_mib")
    assert small.child("learn", "fit")["memory_mib"] == 1536
    tiny = allowance.Allowances("fit", meminfo=meminfo(tmp_path, 1024 * 1024))
    tiny.stopped("learn", "memory_mib")
    assert tiny.child("learn", "fit")["memory_mib"] == 1024  # The floor still holds.
    unknown = allowance.Allowances("fit", meminfo=tmp_path / "missing")
    unknown.stopped("learn", "memory_mib")
    assert unknown.child("learn", "fit")["memory_mib"] == 1024


def test_which_limit_stopped_a_child():
    assert allowance.stopped_by(-24, b"") == "cpu_seconds"
    assert allowance.stopped_by(1, b"Worker refused: CpuBudgetExceeded: x") == "cpu_seconds"
    assert allowance.stopped_by(-25, b"") == "file_mib"
    assert allowance.stopped_by(1, b"Worker refused: MemoryError: ") == "memory_mib"
    assert allowance.stopped_by(-9, b"") is None
    assert allowance.stopped_by(1, b"Worker refused: ValueError: x") is None


def test_budget_argument_round_trips_and_is_bounded():
    budget = {"memory_mib": 2048, "cpu_seconds": 1800, "file_mib": 256}
    assert allowance.parse(allowance.argument(budget), "fit") == budget
    for token in ("budget=1,2", "budget=a,b,c", "budget=0,1,1", "budget=99999,1,1", "x=1,1,1"):
        with pytest.raises(ValueError):
            allowance.parse(token, "fit")
    assert allowance.mode_of(("request", "1", "storage")) == "storage"
    assert allowance.mode_of(("request", "1")) == "fit"


def test_child_reads_its_budget_or_falls_back_to_the_floors():
    token = allowance.argument({"memory_mib": 512, "cpu_seconds": 300, "file_mib": 256})
    assert entrypoint._arguments(["e", "r", "7", "display", token], allowance) == (
        "r",
        7,
        "display",
        {"memory_mib": 512, "cpu_seconds": 300, "file_mib": 256},
    )
    assert entrypoint._arguments(["e", "r", "7"], allowance)[3] == allowance.defaults("fit")
    assert entrypoint._arguments(["e", "r", "7", "storage"], allowance)[2] == "storage"
    for argv in (["e", "r"], ["e", "r", "7", "other"], ["e", "r", "7", "storage", "x", token]):
        with pytest.raises(ValueError):
            entrypoint._arguments(argv, allowance)


SCRIPT = r"""
import sys
if sys.argv[1] == 'cpu':
    sys.stderr.write('Worker refused: CpuBudgetExceeded: stop')
    sys.exit(1)
if sys.argv[1] == 'hang':
    import time
    time.sleep(60)
print(' '.join(sys.argv[1:]))
"""


def test_supervisor_passes_and_learns_the_budget(tmp_path):
    script = tmp_path / "child.py"
    script.write_text(SCRIPT)

    def prepare(*args):
        async def ready():
            return args

        return ready

    async def run():
        plan = allowance.Allowances("short", meminfo=meminfo(tmp_path, 8 * 1024 * 1024))
        worker = supervisor.Supervisor(script, deadline=180, allowance=plan)
        out = await worker.run(prepare("ok", "1", "display"), operation="charts")
        assert out.decode().split() == ["ok", "1", "display", "budget=256,120,128"]
        with pytest.raises(supervisor.WorkerFailed):
            await worker.run(prepare("cpu", "1", "display"), operation="charts")
        out = await worker.run(prepare("ok", "1", "display"), operation="charts")
        assert out.decode().split()[-1] == "budget=256,240,128"
        assert worker.status()["active"] is None
        await worker.aclose()

    asyncio.run(run())


@pytest.mark.skipif(sys.platform == "win32", reason="process groups")
def test_supervisor_gives_more_time_after_a_deadline(tmp_path, monkeypatch):
    script = tmp_path / "child.py"
    script.write_text(SCRIPT)
    monkeypatch.setitem(allowance.WALL, "short", allowance.Range(1, 4))

    async def hang():
        return ("hang", "1", "display")

    async def run():
        plan = allowance.Allowances("short", meminfo=meminfo(tmp_path, 8 * 1024 * 1024))
        worker = supervisor.Supervisor(script, deadline=1, allowance=plan)
        task = asyncio.create_task(worker.run(hang, operation="charts"))
        await asyncio.sleep(0.5)
        assert worker.status()["active"]["limits"]["cpu_seconds"] == 120
        with pytest.raises(TimeoutError):
            await task
        assert plan.wall("charts") == 2
        await worker.aclose()

    asyncio.run(run())
