"""Trusted child protection, without changing the test runner's own limits."""

import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import test_tuner  # noqa: F401
from tuner_under_test.worker import limits


@pytest.fixture
def boundary(monkeypatch, tmp_path):
    values = dict.fromkeys(range(5), (-1, -1))

    def set_limit(key, value):
        values[key] = value

    resource = SimpleNamespace(
        RLIM_INFINITY=-1,
        RLIMIT_AS=0,
        RLIMIT_CPU=1,
        RLIMIT_FSIZE=2,
        RLIMIT_CORE=3,
        RLIMIT_NOFILE=4,
        getrlimit=lambda key: values[key],
        setrlimit=Mock(side_effect=set_limit),
    )
    monkeypatch.setitem(sys.modules, "resource", resource)
    monkeypatch.setattr(
        limits,
        "sys",
        SimpleNamespace(platform="linux", flags=SimpleNamespace(isolated=1, no_site=1)),
    )
    system = SimpleNamespace(
        getppid=Mock(return_value=42),
        nice=Mock(return_value=19),
        getpriority=Mock(return_value=19),
        PRIO_PROCESS=0,
    )
    monkeypatch.setattr(limits, "os", system)
    monkeypatch.setattr(limits, "signal", SimpleNamespace(SIGKILL=9))
    libc = SimpleNamespace(prctl=Mock(return_value=0))
    monkeypatch.setattr(limits.ctypes, "CDLL", Mock(return_value=libc))
    score = tmp_path / "oom_score_adj"
    score.write_text("-300")
    monkeypatch.setattr(limits, "Path", lambda _: score)
    return SimpleNamespace(resource=resource, values=values, system=system, libc=libc, score=score)


def budget():
    return limits.Budget(memory_bytes=384 * 1024**2, cpu_seconds=30, file_bytes=1024**2)


def test_installs_and_reports_all_required_controls(boundary):
    report = limits.install(budget(), expected_parent=42)
    assert report["nice"] == 19
    assert report["oom_score_adj"] == 500
    assert report["parent_pid"] == 42
    assert boundary.values == {
        0: (402653184,) * 2,
        1: (30,) * 2,
        2: (1048576,) * 2,
        3: (0, 0),
        4: (64, 64),
    }
    boundary.libc.prctl.assert_called_once_with(1, 9, 0, 0, 0)


@pytest.mark.parametrize("kind", range(5))
def test_existing_stricter_controls_are_never_relaxed(boundary, kind):
    boundary.values[kind] = (0, 0)
    boundary.score.write_text("900")
    report = limits.install(budget(), expected_parent=42)
    assert boundary.values[kind] == (0, 0)
    assert report["oom_score_adj"] == 900


@pytest.mark.parametrize("option", ["memory_bytes", "cpu_seconds", "file_bytes", "open_files"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5, float("inf"), 2**63])
def test_invalid_budgets_are_rejected(option, value):
    values = {"memory_bytes": 384 * 1024**2, "cpu_seconds": 30, "file_bytes": 1024**2}
    values[option] = value
    with pytest.raises(ValueError):
        limits.Budget(**values)


@pytest.mark.parametrize("parent", [0, 1, -1, True, 1.5])
def test_invalid_parent_is_rejected_before_os_changes(boundary, parent):
    with pytest.raises(ValueError):
        limits.install(budget(), expected_parent=parent)
    boundary.libc.prctl.assert_not_called()


@pytest.mark.parametrize(
    "problem",
    [
        "platform",
        "site",
        "isolated",
        "parent",
        "race",
        "prctl",
        "priority",
        "oom",
        "oom_readback",
        "setlimit",
        "readback",
        "resource",
    ],
)
def test_missing_controls_refuse_execution(boundary, problem, monkeypatch):
    if problem == "platform":
        limits.sys.platform = "win32"
    elif problem in {"site", "isolated"}:
        setattr(limits.sys.flags, "no_site" if problem == "site" else "isolated", 0)
    elif problem == "parent":
        boundary.system.getppid.return_value = 43
    elif problem == "race":
        boundary.system.getppid.side_effect = [42, 43]
    elif problem == "prctl":
        boundary.libc.prctl.return_value = -1
    elif problem == "priority":
        boundary.system.getpriority.return_value = 0
    elif problem == "oom":
        boundary.score.write_text("invalid")
    elif problem == "oom_readback":
        score = SimpleNamespace(read_text=Mock(side_effect=["-300", "0"]), write_text=Mock())
        monkeypatch.setattr(limits, "Path", lambda _: score)
    elif problem == "resource":
        monkeypatch.setitem(sys.modules, "resource", None)
    elif problem == "setlimit":
        boundary.resource.setrlimit.side_effect = OSError("Unavailable")
    else:
        boundary.resource.setrlimit.side_effect = None
    with pytest.raises(limits.IsolationUnavailable):
        limits.install(budget(), expected_parent=42)


def test_real_child_installs_limits_or_refuses_unsupported_host(tmp_path):
    module = Path(limits.__file__).resolve()
    script = tmp_path / "probe.py"
    script.write_text("""
import importlib.util, json, os, sys
spec = importlib.util.spec_from_file_location('child_limits', sys.argv[1])
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
try:
    result = module.install(module.Budget(384*1024**2, 30, 1024**2), expected_parent=int(sys.argv[2]))
except module.IsolationUnavailable:
    assert sys.platform != 'linux'
    print(json.dumps({'unsupported': True}))
else:
    assert sys.platform == 'linux'
    assert result['nice'] == 19 and result['oom_score_adj'] >= 500
    try:
        bytearray(400*1024**2)
    except MemoryError:
        result['allocation_blocked'] = True
    else:
        raise AssertionError('Memory cap absent')
    print(json.dumps(result))
""")
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", str(script), str(module), str(os.getpid())],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report.get("allocation_blocked") if sys.platform == "linux" else report["unsupported"]


def test_real_parent_death_kills_and_reaps_protected_child(tmp_path):
    script = tmp_path / "parent_death.py"
    script.write_text("""
import ctypes, importlib.util, json, os, signal, subprocess, sys, time
from pathlib import Path
module_path, role = sys.argv[1:3]
def load():
    spec = importlib.util.spec_from_file_location('child_limits', module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module
if sys.platform != 'linux':
    module = load()
    try:
        module.install(module.Budget(384*1024**2,30,1024**2),expected_parent=os.getppid())
    except module.IsolationUnavailable:
        print(json.dumps({'unsupported':True}))
    else:
        raise AssertionError('Unsupported host accepted')
elif role == 'worker':
    module = load()
    module.install(module.Budget(384*1024**2,30,1024**2),expected_parent=int(sys.argv[3]))
    print('ready',flush=True)
    time.sleep(30)
elif role == 'owner':
    child = subprocess.Popen([sys.executable,'-I','-S','-B',__file__,module_path,'worker',str(os.getpid())],stdout=subprocess.PIPE)
    print(child.pid,flush=True)
    assert child.stdout.readline().strip() == b'ready'
    print('ready',flush=True)
    sys.stdin.readline()
    os._exit(0)
else:
    # Keep the orphan test child reapable without changing pytest or HA state.
    assert ctypes.CDLL(None).prctl(36,1,0,0,0) == 0
    def expired(*args):
        raise TimeoutError('Parent-death probe readiness timed out')
    signal.signal(signal.SIGALRM,expired)
    signal.alarm(10)
    owner = subprocess.Popen([sys.executable,'-I','-S','-B',__file__,module_path,'owner'],stdin=subprocess.PIPE,stdout=subprocess.PIPE)
    child_pid = None
    try:
        child_pid = int(owner.stdout.readline())
        assert owner.stdout.readline().strip() == b'ready'
        owner.stdin.close()
        assert owner.wait(timeout=5) == 0
        deadline = time.monotonic()+5
        while True:
            found,status = os.waitpid(child_pid,os.WNOHANG)
            if found:
                child_pid = None
                assert os.waitstatus_to_exitcode(status) == -signal.SIGKILL
                print(json.dumps({'reaped':True,'signal':signal.SIGKILL}))
                break
            assert time.monotonic() < deadline, 'Protected child survived owner exit'
            time.sleep(.01)
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.wait(timeout=5)
        if child_pid is not None:
            try:
                os.kill(child_pid,signal.SIGKILL)
                os.waitpid(child_pid,0)
            except ProcessLookupError:
                pass
""")
    child = subprocess.Popen(
        [sys.executable, "-I", "-S", "-B", str(script), limits.__file__, "controller"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=sys.platform == "linux",
    )
    try:
        stdout, stderr = child.communicate(timeout=20)
    except BaseException:
        if sys.platform == "linux":
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        else:
            child.kill()
        child.communicate(timeout=5)
        raise
    assert child.returncode == 0, stderr
    report = json.loads(stdout)
    assert report == (
        {"reaped": True, "signal": 9} if sys.platform == "linux" else {"unsupported": True}
    )
