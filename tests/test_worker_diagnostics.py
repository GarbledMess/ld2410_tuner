"""Failure evidence is bounded, private-data-free, and survives abrupt child death."""

import asyncio
import json
import signal
import sys
from pathlib import Path

import pytest
import test_tuner  # noqa: F401
from tuner_under_test.worker import supervisor


def test_sigkill_error_contains_structured_evidence_without_claiming_oom():
    error = supervisor.WorkerFailed(-9, b"", diagnostics={"elapsed_seconds": 12.5})
    assert "SIGKILL" in str(error)
    assert "12.5" in str(error)
    assert error.diagnostics["elapsed_seconds"] == 12.5
    assert "cause unconfirmed" in str(error)


def test_real_abrupt_exit_keeps_parent_observations(tmp_path):
    script = tmp_path / "killed.py"
    script.write_text("""
import importlib.util, os, signal, sys, time
if sys.platform == 'linux':
    spec = importlib.util.spec_from_file_location('child_evidence', sys.argv[1])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.emit('calculating', operation='learn', views=2)
allocation = bytearray(8 * 1024 * 1024)
end = time.monotonic() + 0.8
while time.monotonic() < end:
    pass
if sys.platform == 'linux':
    os.kill(os.getpid(), signal.SIGKILL)
else:
    sys.exit(23)
""")

    async def run():
        worker = supervisor.Supervisor(script)

        async def prepare():
            return (str(Path(supervisor.__file__).with_name("diagnostics.py")),)

        try:
            with pytest.raises(supervisor.WorkerFailed) as failure:
                await worker.run(prepare)
            evidence = failure.value.diagnostics
            assert evidence["elapsed_seconds"] > 0
            assert evidence["wall_deadline_seconds"] == 30
            if sys.platform == "linux":
                assert failure.value.returncode == -signal.SIGKILL
                assert evidence["last_sample"]["rss_kib"] > 8 * 1024
                assert evidence["last_sample"]["cpu_seconds"] > 0
                assert evidence["samples"] >= 1
                assert '"operation":"learn"' in str(failure.value)
                assert '"phase":"calculating"' in str(failure.value)
            json.dumps(evidence, allow_nan=False)
        finally:
            await worker.aclose()

    asyncio.run(run())


def test_child_context_reports_workload_sizes_without_identifiers():
    from tuner_under_test.worker import diagnostics

    request = {
        "operation": "learn",
        "views": {
            "private-device": {
                "days": [{"path": "private-path", "identity": ["1", "2", "3", "25"]}]
            },
            "other": {"days": [{"path": "private-path", "identity": ["1", "2", "3", "25"]}]},
        },
    }
    result = diagnostics.describe(request, 100)
    assert result == {
        "operation": "learn",
        "views": 2,
        "day_references": 2,
        "unique_files": 1,
        "history_file_bytes": 25,
        "request_bytes": 100,
    }
    assert "private" not in json.dumps(result)


def test_cgroup_events_and_last_proc_sample_survive_child_disappearance(tmp_path, monkeypatch):
    from tuner_under_test.worker import diagnostics

    proc, group = tmp_path / "proc", tmp_path / "cgroups"
    (proc / "self").mkdir(parents=True)
    (proc / "123").mkdir()
    (group / "scope").mkdir(parents=True)
    (proc / "self/cgroup").write_text("0::/scope\n")
    (proc / "vmstat").write_text("oom_kill 10\n")
    (proc / "meminfo").write_text("MemAvailable: 4000 kB\n")
    events = group / "scope/memory.events"
    events.write_text("oom 2\noom_kill 1\n")
    (group / "scope/memory.max").write_text("1610612736")
    (group / "scope/memory.current").write_text("10000")
    (proc / "123/status").write_text("VmRSS: 25000 kB\nVmHWM: 30000 kB\nVmSize: 50000 kB\n")
    (proc / "123/stat").write_text("123 (name with spaces) " + " ".join(["0"] * 11 + ["20", "30"]))
    monkeypatch.setattr(diagnostics.sys, "platform", "linux")
    monkeypatch.setattr(diagnostics.os, "sysconf", lambda _: 100, raising=False)
    observation = diagnostics.Observation(proc=proc, cgroups=group)
    observation.start()
    observation.sample(123)
    events.write_text("oom 3\noom_kill 2\n")
    (proc / "123/status").unlink()
    (proc / "123/stat").unlink()
    observation.sample(123)
    result = observation.report()
    assert result["last_sample"]["rss_kib"] == 25000
    assert result["last_sample"]["cpu_seconds"] == 0.5
    assert result["samples"] == 1
    assert result["cgroup_event_delta"] == {"oom": 1, "oom_kill": 1}
    assert result["host_oom_kill_delta"] == 0
    assert result["cgroup_memory_after"]["max_bytes"] == 1610612736
    assert "shared" in result["oom_scope"]


@pytest.mark.parametrize("code,name", [(-9, "SIGKILL"), (-11, "SIGSEGV"), (-24, "SIGXCPU")])
def test_negative_exits_report_signal_names(code, name):
    assert name in str(supervisor.WorkerFailed(code, b""))


def test_diagnostic_probe_failure_is_explicit_and_nonfatal():
    from tuner_under_test.worker import diagnostics

    def unavailable():
        raise PermissionError("private path must not escape")

    assert diagnostics.safely(unavailable) == {"observation_error": "PermissionError"}


def test_unavailable_proc_and_malformed_cgroup_are_explicit(tmp_path, monkeypatch):
    from tuner_under_test.worker import diagnostics

    monkeypatch.setattr(diagnostics.sys, "platform", "linux")
    (tmp_path / "self").mkdir()
    (tmp_path / "self/cgroup").write_text("bad\n0::/../../outside\n")
    observation = diagnostics.Observation(proc=tmp_path, cgroups=tmp_path)
    observation.start()
    observation.sample(999)
    report = observation.report()
    assert report["samples"] == 0
    assert report["host_oom_kill_delta"] is None
    assert report["cgroup_event_delta"] == {}
    assert report["host_available_kib"] is None
    assert observation.group == tmp_path
    assert "unknown signal" in str(supervisor.WorkerFailed(-9999, b""))


def test_storage_context_and_unknown_operation_never_echo_input():
    from tuner_under_test.worker import diagnostics

    report = diagnostics.describe({"operation": "private-value"}, 32)
    assert report["operation"] == "unknown"
    assert report["views"] == 0
    assert diagnostics.describe({"operation": "maintenance"}, 64)["operation"] == "maintenance"


def test_child_milestone_is_small_and_contains_resource_use(monkeypatch):
    from types import SimpleNamespace

    from tuner_under_test.worker import diagnostics

    monkeypatch.setitem(
        sys.modules,
        "resource",
        SimpleNamespace(
            RUSAGE_SELF=0,
            getrusage=lambda _: SimpleNamespace(ru_utime=2.5, ru_stime=0.5, ru_maxrss=1234),
        ),
    )
    records = []
    monkeypatch.setattr(diagnostics.os, "write", lambda fd, data: records.append((fd, data)))
    diagnostics.emit("calculating", operation="learn")
    fd, raw = records[0]
    assert fd == 2 and len(raw) < 256
    assert raw.startswith(diagnostics.PREFIX.encode())
    assert json.loads(raw[len(diagnostics.PREFIX) :]) == {
        "phase": "calculating",
        "operation": "learn",
        "cpu_seconds": 3.0,
        "peak_rss_kib": 1234,
    }
