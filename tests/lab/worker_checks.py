"""Run explicit synthetic probe bundles against their matching mounted sources."""

import hashlib
import json
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

reports = Path("/config/lab-reports")
reports.mkdir(exist_ok=True)
stop = threading.Event()
latencies, errors = [], []


def heartbeat():
    while not stop.is_set():
        started = time.perf_counter()
        try:
            try:
                with urllib.request.urlopen(
                    "http://127.0.0.1:8123/api/onboarding", timeout=2
                ) as response:
                    status = response.status
            except urllib.error.HTTPError as response:
                status = response.code
            assert status == 200, status
            latencies.append((time.perf_counter() - started) * 1000)
        except Exception as error:
            errors.append(type(error).__name__)
        stop.wait(0.1)


thread = threading.Thread(target=heartbeat)
thread.start()
try:
    with tempfile.TemporaryDirectory(prefix="synthetic-workers-") as temporary:
        target = Path(temporary)
        with zipfile.ZipFile(sys.argv[1]) as archive:
            assert len(archive.infolist()) <= 200
            assert sum(item.file_size for item in archive.infolist()) < 10 * 1024**2
            hashes = json.loads(archive.read("SOURCE-HASHES.json"))
            assert set(archive.namelist()) == set(hashes) | {"SOURCE-HASHES.json"}
            for name, expected in hashes.items():
                assert not Path(name).is_absolute() and ".." not in Path(name).parts
                data = archive.read(name)
                assert hashlib.sha256(data).hexdigest() == expected
                if name.startswith("ld2410_tuner/"):
                    source = Path("/workspace/project/custom_components") / name
                    assert source.read_bytes() == data, "Stale probe bundle: " + name
            archive.extractall(target)
        for name in ["hacs-worker-investigation.py", "supervisor-check.py"]:
            result = subprocess.run(
                [sys.executable, "-I", "-S", "-B", "-W", "error", str(target / name)],
                capture_output=True,
                text=True,
                timeout=180,
                cwd=target,
            )
            assert result.returncode == 0, result.stderr
            assert not result.stderr, result.stderr
            report = json.loads(result.stdout)
            (reports / (name + ".json")).write_text(json.dumps(report, indent=2))
            print(name, json.dumps(report), flush=True)
finally:
    stop.set()
    thread.join(timeout=5)
    ordered = sorted(latencies)
    latency = {
        "successful": len(ordered),
        "errors": errors,
        "median_ms": statistics.median(ordered) if ordered else None,
        "p95_ms": ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))] if ordered else None,
        "max_ms": max(ordered) if ordered else None,
    }
    (reports / "http-during-workers.json").write_text(json.dumps(latency, indent=2))
    print("HA HTTP during worker probes:", json.dumps(latency), flush=True)
assert latencies and not errors, "HA HTTP failures during worker probes"
