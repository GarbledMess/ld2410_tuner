"""Deterministic raw day files for scaling checks; no recorded history is used."""

from array import array
from pathlib import Path

DAY = 86400
START = 1_790_000_000 // DAY * DAY
CHANNELS = tuple(range(18))


def _labels(start):
    return [
        {
            "start": start + hour * 3600,
            "end": start + (hour + 2) * 3600,
            "state": "present" if hour in (6, 10) else "not_present",
        }
        for hour in (6, 8, 10, 12)
    ]


def _times(density):
    step = 6 / density
    for row in range(DAY * density // 6):
        offset = row * step
        if 10800 <= offset < 10860:
            continue
        yield offset
        if offset % 900 == 0:
            yield offset + 0.125
            yield offset + 0.25


def _energy(radar, channel, offset, day):
    # Pairs share the same waveform; alternate pairs have opposite responses.
    occupied = int(offset) // 3600 in (6, 7, 10, 11, 18, 19)
    active = occupied if radar % 4 < 2 else not occupied
    noise = (int(offset * 8) * 17 + channel * 11 + radar // 2 + day * 7) % 9
    value = 8 + noise + (35 + channel % 5 if active else 0)
    return 95 if int(offset) % 7200 < 6 else value


def _day(radar, day, density):
    from tuner_under_test.history import dayfile

    start = START + day * DAY
    offsets = tuple(_times(density))
    times = array("d", (start + offset for offset in offsets))
    ends = array("d", (*times[1:], start + DAY))
    before_gap = times.index(start + 10860) - 1
    ends[before_gap] = start + 10800
    columns = [
        bytes(_energy(radar, channel, offset, day) for offset in offsets) for channel in CHANNELS
    ]
    return dayfile.Day(start, start + DAY, times, ends, columns, [])


def _live(store, rid, day):
    from tuner_under_test.history import dayfile

    for index, (start, end) in enumerate(zip(day.times, day.ends, strict=True)):
        store.append(rid, dayfile.Change(start, dict(enumerate(day.vector(index)))))
        if end < day.end and end < day.times[index + 1]:
            store.append(rid, dayfile.End(end))


def _check_dimensions(radars, days, density):
    for name, value in (("radars", radars), ("days", days), ("density", density)):
        if type(value) is not int or value < 1:
            raise ValueError(f"{name} must be a positive integer")


def build_fixture(root, radars=30, days=28, *, density=1):
    """Write one day at a time, keeping only each radar's final day live.

    Each day covers midnight to midnight except one 60-second outage. Labels
    cover 06:00-14:00. Base rows are six seconds apart (three at density two),
    with two 125-ms burst rows every 15 minutes. No cell digests are prepared.
    """
    from tuner_under_test.history import daystore

    _check_dimensions(radars, days, density)
    store = daystore.DayStore(root / "history", cache_bytes=0)
    devices, rows = {}, 0
    labels = [label for day in range(days) for label in _labels(START + day * DAY)]
    for radar in range(radars):
        rid = f"r{radar}"
        devices[rid] = {"history_labels": labels, "training_state": "unknown"}
        for index in range(days):
            day = _day(radar, index, density)
            rows += len(day.times)
            if index == days - 1:
                _live(store, rid, day)
            else:
                store.replace(rid, daystore.date_of(day.start), day)
    return {
        "store": store,
        "devices": devices,
        "radar_days": radars * days,
        "source_rows": rows,
        "base_cadence_seconds": 6 / density,
        "labelled_duty_cycle": 1 / 3,
    }


def _request(fixture, directory, days):
    from types import SimpleNamespace

    from tuner_under_test.const import HISTORY_KEYS
    from tuner_under_test.worker import capture, codec, manifest, protocol

    end = START + days * DAY
    view = SimpleNamespace(
        data={"devices": fixture["devices"], "room_snapshot": None},
        _days=fixture["store"],
        _history_until=end,
        _snapshot_history_start=START,
        _physical_history_start=START,
        _attribution_until=end,
    )
    with manifest.Writer(directory / "history.sqlite") as writer:
        capture.prepare_multi({"all": view}, directory, sink=writer)
    request = {"manifest": str(writer.path)}
    members = {
        rid: {"view_key": "all", "thresholds": dict.fromkeys(HISTORY_KEYS, 50), "timing": {}}
        for rid in fixture["devices"]
    }
    labels = [{**label, "source": "manual"} for label in fixture["devices"]["r0"]["history_labels"]]
    room = {"group_id": "room", "groups": {}, "room_labels": {"room": labels}}
    request.update(
        operation="room_fit",
        parameters=codec.encode(
            {
                "members": {"room": members},
                "rooms": {"room": room},
                "start": START,
                "end": end,
                "weight": 0.0,
            }
        ),
    )
    path = directory / "request.json"
    path.write_bytes(protocol.pack("learn", request))
    return path


async def _launch(path, trace, spill_signal=None):
    import json
    import os

    from tuner_under_test.worker.supervisor import Supervisor

    supervisor = Supervisor(Path(__file__), deadline=1800, stdout_bytes=1024 * 1024)

    async def prepare():
        return str(path), str(os.getpid()), str(int(trace)), str(spill_signal or "")

    try:
        return json.loads(await supervisor.run(prepare))
    finally:
        await supervisor.aclose()


def run_scaling(root: Path, radars=30, days=28, *, warm=False, trace=False, density=1):
    """Generate outside the timer, capture once, then run complete fresh child jobs.

    Warm means a second child using sidecars produced by the first child. Windows
    exercises the stdlib job but cannot install Linux protection; this is explicit
    in the result. The Linux path always installs the production worker limits.
    """
    import asyncio

    root.mkdir(parents=True, exist_ok=True)
    fixture = build_fixture(root, radars, days, density=density)
    return asyncio.run(_run_fixture(fixture, root, days, warm, trace))


def run_limit_checks():
    """Exercise actual kernel ceilings in a disposable protected Linux child."""
    import asyncio

    return asyncio.run(_launch(Path("--probe-limits"), False))


def _probe_file_limit(limit):
    import errno
    import signal
    import tempfile

    signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
    with tempfile.TemporaryFile() as stream:
        try:
            stream.truncate(limit + 1)
        except OSError as error:
            if error.errno == errno.EFBIG:
                return True
            raise
    raise RuntimeError("Kernel did not enforce the file-size ceiling")


def _probe_descriptor_limit(limit):
    import errno
    import os
    from contextlib import ExitStack

    with ExitStack() as stack:
        for count in range(limit + 1):
            try:
                stack.enter_context(open(os.devnull, "rb"))
            except OSError as error:
                if error.errno == errno.EMFILE:
                    return count
                raise
    raise RuntimeError("Kernel did not enforce the descriptor ceiling")


async def _run_fixture(fixture, root, days, warm, trace, *, pause_at_spill=False):
    """Keep captures owned until preparation and every child have finished cleanup."""
    import asyncio
    import tempfile

    from tuner_under_test.worker.supervisor import owned_preparation

    with tempfile.TemporaryDirectory(prefix="scaling-job-", dir=root) as temporary:
        path = await owned_preparation(asyncio.to_thread(_request, fixture, Path(temporary), days))
        signal = Path(temporary) / "spill-ready" if pause_at_spill else None
        cold = await _launch(path, trace if not warm else False, signal)
        result = await _launch(path, trace) if warm else cold
        result.update(
            mode="warm" if warm else "cold",
            radar_days=fixture["radar_days"],
            source_rows=fixture["source_rows"],
            base_cadence_seconds=fixture["base_cadence_seconds"],
            labelled_duty_cycle=fixture["labelled_duty_cycle"],
        )
        if warm:
            result["cold"] = cold
        return result


def _namespace():
    import sys
    from types import ModuleType

    package = ModuleType("tuner_under_test")
    package.__path__ = [str(Path(__file__).resolve().parents[1] / "custom_components/ld2410_tuner")]
    sys.modules[package.__name__] = package


def _measure_reads():
    """Count actual worker reads without retaining blocks or changing their order."""
    from tuner_under_test.history import cell_cache, fit_rows

    metrics = {"max_row_block": 0, "raw_rows_read": 0, "cache_hits": 0}
    blocks = fit_rows.CapturedRowSource.blocks
    load = cell_cache.load

    def counted_blocks(source, lo, hi):
        for block in blocks(source, lo, hi):
            count = len(block.times)
            metrics["max_row_block"] = max(metrics["max_row_block"], count)
            metrics["raw_rows_read"] += count
            yield block

    def counted_load(*args, **kwargs):
        digest = load(*args, **kwargs)
        if digest is not None:
            metrics["cache_hits"] += 1
        return digest

    fit_rows.CapturedRowSource.blocks = counted_blocks
    cell_cache.load = counted_load
    return metrics


def _measure_digests():
    """Track live digest payloads by identity, without retaining the digests.

    Bytes count channel/coverage buffers and energy-index arrays; Python object
    overhead and physical-row spools are measured separately by tracemalloc.
    """
    from tuner_under_test.engine.hybrid.digest import DayDigest

    live = {}
    metrics = {"max_resident_digests": 0, "resident_digest_bytes": 0}
    initialize = DayDigest.__init__

    def counted_init(digest, *args, **kwargs):
        initialize(digest, *args, **kwargs)
        size = sum(map(len, digest.channels)) + len(digest.cov)
        if digest.index is not None:
            size += digest.index.nbytes()
        live[id(digest)] = size
        metrics["max_resident_digests"] = max(metrics["max_resident_digests"], len(live))
        metrics["resident_digest_bytes"] = max(metrics["resident_digest_bytes"], sum(live.values()))

    def released(digest):
        live.pop(id(digest), None)

    DayDigest.__init__ = counted_init
    DayDigest.__del__ = released
    return metrics, live


def _measure_spills(spill_signal=None):
    """High-water bytes in temporary streams, including rolled spools and replay.

    Persistent sidecars and SQLite metadata are excluded. Tracking the underlying
    disk file counts a rolled spool once, even when its contents are copied out.
    The optional cancellation-test barrier pauses with a real spill open; normal
    measurements never supply it.
    """
    import tempfile

    live = {}
    metrics = {"spill_bytes": 0, "max_spill_files": 0, "open_spill_files": 0}
    original = tempfile.TemporaryFile

    class CountedFile:
        def __init__(self, *args, **kwargs):
            self.stream = original(*args, **kwargs)
            live[id(self)] = 0
            metrics["open_spill_files"] = len(live)
            metrics["max_spill_files"] = max(metrics["max_spill_files"], len(live))

        def __getattr__(self, name):
            return getattr(self.stream, name)

        def write(self, value):
            count = self.stream.write(value)
            live[id(self)] = max(live[id(self)], self.stream.tell())
            metrics["spill_bytes"] = max(metrics["spill_bytes"], sum(live.values()))
            if spill_signal is not None:
                self.stream.flush()
                _pause_at_spill(spill_signal, metrics["spill_bytes"])
            return count

        def truncate(self, size=None):
            size = self.stream.truncate(size)
            live[id(self)] = size
            metrics["spill_bytes"] = max(metrics["spill_bytes"], sum(live.values()))
            return size

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

        def close(self):
            try:
                self.stream.close()
            finally:
                live.pop(id(self), None)
                metrics["open_spill_files"] = len(live)

        def __del__(self):
            if "stream" in self.__dict__:
                self.close()

    tempfile.TemporaryFile = CountedFile
    return metrics


def _pause_at_spill(path, size):
    """Cancellation-test barrier; the owner cancels the paused child, then reaps it."""
    import time

    path.write_text(str(size), encoding="utf-8")
    while path.exists():
        time.sleep(0.01)


def _child():
    import json
    import os
    import sys
    import time
    import tracemalloc

    _namespace()
    from tuner_under_test.worker.limits import Budget, install

    controls = None
    if sys.platform == "linux":
        controls = install(
            Budget(512 * 1024**2, 120, 128 * 1024**2), expected_parent=int(sys.argv[2])
        )
    if sys.argv[1] == "--probe-limits":
        if controls is None:
            raise RuntimeError("Kernel limit probes require Linux")
        result = {
            "file_limit_enforced": _probe_file_limit(controls["file_bytes"]),
            "descriptors_opened": _probe_descriptor_limit(controls["open_files"]),
            "limits": controls,
        }
        print(json.dumps(result))
        return
    if sys.argv[3] == "1":
        tracemalloc.start()
    started = time.process_time()
    from tuner_under_test.worker import calculations, protocol

    spill_signal = Path(sys.argv[4]) if len(sys.argv) > 4 and sys.argv[4] else None
    spill_metrics = _measure_spills(spill_signal)
    metrics = _measure_reads()
    digest_metrics, live_digests = _measure_digests()
    request = protocol.unpack(Path(sys.argv[1]).read_bytes(), "learn")
    report = calculations.execute(request)
    elapsed = time.process_time() - started
    peak = tracemalloc.get_traced_memory()[1] if tracemalloc.is_tracing() else 0
    thresholds = report["thresholds"]
    result = {
        "pid": os.getpid(),
        "isolated": bool(sys.flags.isolated),
        "no_site": bool(sys.flags.no_site),
        "no_bytecode": bool(sys.dont_write_bytecode),
        "limits": controls,
        "cpu_seconds": elapsed,
        "traced_peak_bytes": peak,
        "thresholds": thresholds,
        "cost_before": report["cost_before"],
        "cost_after": report["cost_after"],
        **metrics,
        **digest_metrics,
        **spill_metrics,
        "resident_digests_at_end": len(live_digests),
        "legality": all(
            type(value) is int and 0 <= value <= 100
            for values in thresholds.values()
            for value in values.values()
        ),
    }
    print(json.dumps(result))


if __name__ == "__main__":
    _child()
