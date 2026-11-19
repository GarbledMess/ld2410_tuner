"""Worker limits come from what Home Assistant can spare, never from what a job wants."""

import asyncio
import sys
from pathlib import Path

import pytest
import test_tuner  # noqa: F401
from tuner_under_test.worker import allowance, entrypoint, supervisor


def host(total=4096, free=3000, stall=0.0):
    return allowance.Host(total_mib=total, free_mib=free, memory_stall=stall)


def test_memory_limit_is_a_share_of_what_home_assistant_can_spare():
    # 4 GiB host: Home Assistant keeps 409 MiB; the child may use half of the rest.
    assert allowance.plan("fit", host(free=3000)) == {
        "memory_mib": 1295,
        "cpu_seconds": 900,
        "file_mib": 128,
    }
    assert allowance.plan("fit", host(total=16384, free=15000))["memory_mib"] == 2048
    assert allowance.plan("display", host(free=1000))["memory_mib"] == 295
    # Address space is reserved, not used: below the share, the floor still applies.
    assert allowance.plan("fit", host(free=1000))["memory_mib"] == 1024


def test_no_room_means_wait():
    assert allowance.plan("fit", host(free=409 + 255)) is None  # Reserve plus too little.
    assert allowance.plan("fit", host(free=409 + 256)) is not None
    assert allowance.plan("fit", host(free=3000, stall=25.0)) is None
    assert allowance.plan("storage", None) == allowance.defaults("storage")  # Unmeasurable.


def test_host_is_read_from_meminfo_cgroup_and_pressure(tmp_path):
    proc, cgroups = tmp_path / "proc", tmp_path / "cgroup"
    (proc / "self").mkdir(parents=True)
    (proc / "pressure").mkdir()
    (proc / "meminfo").write_text("MemTotal: 4194304 kB\nMemAvailable: 3145728 kB\n")
    (proc / "self/cgroup").write_text("0::/ha\n")
    (proc / "pressure/memory").write_text(
        "some avg10=12.50 avg60=1.00 avg300=0.10 total=1\nfull avg10=0.00 avg60=0 total=0\n"
    )
    (cgroups / "ha").mkdir(parents=True)
    assert allowance.read_host(proc, cgroups) == host(total=4096, free=3072, stall=12.5)
    (cgroups / "ha/memory.max").write_text(str(2048 * allowance.MIB))
    (cgroups / "ha/memory.current").write_text(str(1536 * allowance.MIB))
    assert allowance.read_host(proc, cgroups).free_mib == 512  # The container is tighter.
    assert allowance.read_host(tmp_path / "missing", cgroups) is None


def test_governor_pauses_on_sustained_lag_and_resumes_when_calm():
    governor = allowance.Governor(max_pause=100)
    busy, calm = 0.3, 0.01
    assert [governor.decide(busy, None, t) for t in range(3)] == [None] * 3
    assert governor.decide(busy, None, 3) == "pause"
    assert governor.decide(calm, None, 4) is None
    assert governor.decide(busy, None, 5) is None  # Calm must be continuous.
    assert governor.decide(calm, None, 6) is None
    assert governor.decide(calm, None, 11) == "resume"
    assert governor.last_pause == 8
    assert governor.decide(busy, None, 12) is None  # Needs a fresh second of lag.


def test_governor_pausing_is_bounded_and_memory_shortage_stops():
    governor = allowance.Governor(max_pause=10)
    for t in range(4):
        governor.decide(0.3, None, t)
    assert governor.decide(0.3, None, 13) == "resume"  # Paused 10 s: runs on.
    for t in range(14, 20):
        assert governor.decide(0.3, None, t) is None
    assert governor.decide(0.0, host(free=100), 20) == "stop"
    saving = allowance.Governor(max_pause=10, stoppable=False)
    assert saving.decide(0.0, host(free=100), 0) is None


def test_budget_argument_round_trips_and_is_bounded():
    budget = {"memory_mib": 2048, "cpu_seconds": 900, "file_mib": 128}
    assert allowance.parse(allowance.argument(budget), "fit") == budget
    for token in ("budget=1,2", "budget=a,b,c", "budget=0,1,1", "budget=4096,1,1", "x=1,1,1"):
        with pytest.raises(ValueError):
            allowance.parse(token, "fit")
    assert allowance.mode_of(("request", "1", "storage")) == "storage"
    assert allowance.mode_of(("request", "1")) == "fit"
    assert allowance.mode_for("flush") == "storage"
    assert allowance.mode_for("charts") == "display"
    assert allowance.mode_for("learn") == "fit"


def test_child_reads_its_budget_or_falls_back_to_the_floors():
    token = allowance.argument({"memory_mib": 300, "cpu_seconds": 60, "file_mib": 64})
    assert entrypoint._arguments(["e", "r", "7", "display", token], allowance) == (
        "r",
        7,
        "display",
        {"memory_mib": 300, "cpu_seconds": 60, "file_mib": 64},
    )
    assert entrypoint._arguments(["e", "r", "7"], allowance)[3] == allowance.defaults("fit")
    assert entrypoint._arguments(["e", "r", "7", "storage"], allowance)[2] == "storage"
    for argv in (["e", "r"], ["e", "r", "7", "other"], ["e", "r", "7", "storage", "x", token]):
        with pytest.raises(ValueError):
            entrypoint._arguments(argv, allowance)


class FakePolicy:
    def __init__(self, *hosts):
        self.hosts = list(hosts)

    def host(self):
        return self.hosts.pop(0) if len(self.hosts) > 1 else self.hosts[0]


SCRIPT = r"""
import sys, time
if sys.argv[1] == 'spin':
    end = time.monotonic() + float(sys.argv[2])
    while time.monotonic() < end:
        time.sleep(0.01)
print(' '.join(sys.argv[1:]))
"""


def prepare(*args):
    async def ready():
        return args

    return ready


@pytest.fixture
def script(tmp_path):
    path = tmp_path / "child.py"
    path.write_text(SCRIPT)
    return path


def test_supervisor_sizes_the_child_from_free_memory(script):
    async def run():
        worker = supervisor.Supervisor(script, allowance=FakePolicy(host(free=3000)))
        out = await worker.run(prepare("ok", "1", "display"), operation="charts")
        assert out.decode().split() == ["ok", "1", "display", "budget=512,120,128"]
        await worker.aclose()

    asyncio.run(run())


def test_jobs_wait_for_room_and_charts_give_up(script, monkeypatch):
    monkeypatch.setattr(allowance, "WAIT_POLL", 0.01)
    monkeypatch.setitem(
        allowance.MODES,
        "display",
        allowance.MODES["display"].__class__(allowance.Range(256, 512), 120, 128, 0.02, False),
    )

    async def run():
        tight = host(free=500)
        policy = FakePolicy(tight, tight, host(free=3000))
        worker = supervisor.Supervisor(script, allowance=policy)
        out = await worker.run(prepare("ok", "1"), operation="learn")
        assert out.decode().split()[-1] == "budget=1295,900,128"
        worker.allowance = FakePolicy(tight)
        with pytest.raises(supervisor.NoRoom):
            await worker.run(prepare("ok", "1", "display"), operation="charts")
        assert issubclass(supervisor.NoRoom, supervisor.Busy)
        await worker.aclose()

    asyncio.run(run())


@pytest.mark.skipif(sys.platform != "linux", reason="process groups and /proc")
def test_guard_pauses_without_spending_the_deadline_and_stops_on_shortage(script, monkeypatch):
    monkeypatch.setattr(allowance, "GUARD_INTERVAL", 0.02)
    states = []

    def state(pid):
        return Path(f"/proc/{pid}/stat").read_text().rpartition(") ")[2][0]

    async def run():
        worker = supervisor.Supervisor(script, deadline=5, allowance=FakePolicy(host()))
        actions = iter(["pause", None, None, None, None, "resume"])
        original = worker._act

        def act(process, scope, governor, _action):
            action = next(actions, None)
            if action == "resume":
                states.append(state(process.pid))
                governor.last_pause = 2.0
            original(process, scope, governor, action)

        worker._act = act
        await worker.run(prepare("spin", "0.3"), operation="learn")
        assert states == ["T"]  # Stopped (paused) before it resumed.

        worker._act = original
        worker.allowance = FakePolicy(host(), host(free=100))
        with pytest.raises(supervisor.StoppedForHomeAssistant, match="enough memory"):
            await worker.run(prepare("spin", "5"), operation="learn")
        await worker.aclose()

    asyncio.run(run())


@pytest.mark.skipif(sys.platform != "linux", reason="process groups and /proc")
def test_paused_time_extends_the_deadline(script, monkeypatch):
    monkeypatch.setattr(allowance, "GUARD_INTERVAL", 0.02)
    monkeypatch.setattr(allowance, "LAG_PAUSE", -1.0)  # Always "busy": pause at once.
    monkeypatch.setattr(allowance, "LAG_SAMPLES", 1)
    monkeypatch.setattr(allowance, "LAG_RESUME", -1.0)  # Never calm: resume at max_pause.

    async def run():
        worker = supervisor.Supervisor(script, deadline=0.6, allowance=FakePolicy(host()))
        # Spins 0.4 s of its own, but is paused up to the 0.6 s deadline first.
        out = await worker.run(prepare("spin", "0.4"), operation="learn")
        assert out.startswith(b"spin")
        await worker.aclose()

    asyncio.run(run())
