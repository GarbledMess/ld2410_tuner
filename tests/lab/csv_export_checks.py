"""A dense day's CSV through a real Home Assistant: the old page route against the file route.

Run in a fresh disposable HA (it onboards). One radar's finished day of 142,000 readings (the
size of a busy real day) is stored before the integration is added. The day is then read in
10,000-row pages over the websocket, as the panel's export did, and exported as one file
downloaded from its signed link. For each: wall time, CPU of the HA process with its reaped
worker children, HA's resident memory, HA's HTTP latency from a separate process. The file
must equal the CSV of the pages byte for byte. Synthetic data only; prints one JSON report.
"""

import asyncio
import datetime
import io
import json
import random
import statistics
import sys
import time
import types
from pathlib import Path

import aiohttp

package = types.ModuleType("probe_tuner")
package.__path__ = [str(Path(__file__).resolve().parents[2] / "custom_components/ld2410_tuner")]
sys.modules[package.__name__] = package
sys.path.insert(0, str(Path(__file__).resolve().parent))
from export_checks import BASE, CONFIG, add_integration, command, onboard  # noqa: E402
from probe_tuner.const import (  # noqa: E402
    DOMAIN,
    STORAGE_KEY,
    STORAGE_MINOR_VERSION,
    STORAGE_VERSION,
)
from probe_tuner.history import dayfile, daystore  # noqa: E402
from probe_tuner.presentation import recorded_csv  # noqa: E402

ROWS = 142_000
STORE = CONFIG / ".storage"  # The disposable lab's own HA store; synthetic data only.
HEARTBEAT = """
import sys, time, urllib.error, urllib.request
deadline = float(sys.argv[1])
while time.time() < deadline:
    started = time.monotonic()
    try:
        urllib.request.urlopen("http://127.0.0.1:8123/api/", timeout=10).close()
    except urllib.error.HTTPError:
        pass
    print(time.monotonic() - started, flush=True)  # Each sample, so stopping loses none.
    time.sleep(0.05)
"""


def _ha_pid():
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            try:
                if b"homeassistant" in (entry / "cmdline").read_bytes():
                    return int(entry.name)
            except OSError:
                continue
    raise RuntimeError("Home Assistant process not found")


def _cpu(pid):
    """utime + stime + reaped children's, in seconds."""
    fields = (Path("/proc") / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
    return sum(int(fields[i]) for i in (11, 12, 13, 14)) / 100


def _rss_mib(pid):
    for line in (Path("/proc") / str(pid) / "status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) / 1024
    return 0.0


def _store_day(date):
    rng = random.Random(3)
    start, _end = daystore.bounds(date)
    builder = dayfile.DayBuilder(*daystore.bounds(date))
    moment = start + 10
    for n in range(ROWS):
        moment += rng.choice((0.04, 0.2, 0.5, 1.0))
        gates = {g: rng.randint(0, 100) for g in rng.sample(range(18), 4)}
        if n % 40 == 0:
            gates[3] = rng.uniform(0, 100)
        builder.add(dayfile.Change(moment, gates))
    builder.add(dayfile.End(moment + 1))
    store = daystore.DayStore(STORE / DOMAIN / "history")
    path = store._path("probe", date, ".day")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(dayfile.encode_day(builder.day()))


async def _measured(pid, work):
    beat = await asyncio.create_subprocess_exec(
        sys.executable, "-c", HEARTBEAT, str(time.time() + 600), stdout=asyncio.subprocess.PIPE
    )
    peak, done = [_rss_mib(pid)], asyncio.Event()

    async def sample():
        while not done.is_set():
            peak.append(_rss_mib(pid))
            await asyncio.sleep(0.1)

    sampler = asyncio.create_task(sample())
    cpu, wall = _cpu(pid), time.monotonic()
    try:
        result = await work()
    finally:
        wall, cpu = time.monotonic() - wall, _cpu(pid) - cpu
        done.set()
        await sampler
        beat.terminate()
        latencies = [float(line) for line in (await beat.communicate())[0].split()]
    p95 = statistics.quantiles(latencies, n=20)[18] if len(latencies) > 2 else None
    return result, {
        "wall_seconds": round(wall, 2),
        "ha_cpu_seconds": round(cpu, 2),
        "ha_rss_before_mib": round(peak[0], 1),
        "ha_rss_peak_mib": round(max(peak), 1),
        "http_ms_p95": None if p95 is None else round(p95 * 1000, 1),
        "http_ms_max": round(max(latencies) * 1000, 1) if latencies else None,
    }


async def _pages(socket, date, pinned):
    rows, offset, ident, count = [], 0, 600, 0
    while offset is not None:
        message = {
            "type": f"{DOMAIN}/recording_rows",
            "device_id": "probe",
            "date": date,
            "offset": offset,
            "limit": 10000,
            **pinned,
        }
        reply = await command(socket, ident, message, timeout=300)
        assert reply.get("success"), reply.get("error")
        rows += reply["result"]["rows"]
        offset, ident, count = reply["result"]["next_offset"], ident + 1, count + 1
    return rows, count


async def _file(session, socket, date, pinned):
    message = {"type": f"{DOMAIN}/recording_csv", "device_id": "probe", "date": date, **pinned}
    reply = await command(socket, 900, message, timeout=300)
    assert reply.get("success"), reply.get("error")
    async with session.get(BASE + reply["result"]["url"]) as response:  # The signed link alone.
        assert response.status == 200
        return reply["result"], await response.read()


async def _connected(session, token):
    socket = await session.ws_connect(BASE + "/api/websocket", max_msg_size=0)
    await socket.receive_json()
    await socket.send_json({"type": "auth", "access_token": token})
    assert (await socket.receive_json())["type"] == "auth_ok"
    for ident in range(1, 120):  # The runtime starts in the background after setup.
        if (await command(socket, ident, {"type": f"{DOMAIN}/snapshot"})).get("success"):
            return socket
        await asyncio.sleep(1)
    raise TimeoutError("the integration's runtime did not start")


async def main():
    yesterday = datetime.datetime.now(datetime.UTC).date() - datetime.timedelta(days=1)
    date = yesterday.isoformat()
    saved = {
        "version": STORAGE_VERSION,
        "minor_version": STORAGE_MINOR_VERSION,
        "key": STORAGE_KEY,
        "data": {"devices": {"probe": {"training_state": "unknown"}}},
    }
    report = {"rows": ROWS}
    async with aiohttp.ClientSession() as session:
        token = await onboard(session)
        STORE.mkdir(exist_ok=True)
        (STORE / STORAGE_KEY).write_text(json.dumps(saved), encoding="utf-8")
        _store_day(yesterday)
        await add_integration(session, {"Authorization": "Bearer " + token})
        pid = _ha_pid()
        socket = await _connected(session, token)
        try:
            message = {
                "type": f"{DOMAIN}/recording_rows",
                "device_id": "probe",
                "date": date,
                "limit": 1,
            }
            first = (await command(socket, 500, message))["result"]
            pinned = {"end": first["end"], "revision": first["revision"]}
            await asyncio.sleep(5)  # Let startup work settle before measuring.
            (rows, count), report["pages"] = await _measured(
                pid, lambda: _pages(socket, date, pinned)
            )
            (result, body), report["file"] = await _measured(
                pid, lambda: _file(session, socket, date, pinned)
            )
        finally:
            await socket.close()
    expected = io.StringIO(newline="")
    expected.write(recorded_csv.HEADER)
    recorded_csv.write_rows(expected, rows)
    report["pages"]["requests"] = count
    report["file"]["requests"] = 1
    report["page_rows"], report["file_rows"] = len(rows), result["rows"]
    report["csv_bytes"] = len(body)
    report["identical"] = body == expected.getvalue().encode()
    print(json.dumps(report, indent=1), flush=True)
    assert report["identical"], "the file differs from the paged CSV"
    assert result["rows"] == len(rows) == ROWS


if __name__ == "__main__":
    asyncio.run(main())
