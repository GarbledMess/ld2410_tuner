"""Lossless bounded raw recording pages."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
import test_tuner  # noqa: F401
from test_event_charts import row, view
from tuner_under_test.worker.recordings import unpack_page


@pytest.mark.parametrize("date", ["../1970-01-01", "19700101", "1970-1-1", "2025-02-30"])
def test_dates_are_strict(date):
    from tuner_under_test.presentation.recordings import day_bounds

    with pytest.raises(ValueError):
        day_bounds(date)


def test_exact_pages_and_transport(tmp_path):
    import json
    import subprocess
    import sys
    from pathlib import Path

    from tuner_under_test.presentation.recordings import raw_view
    from tuner_under_test.worker import calculations, capture, codec, protocol

    source = view(
        [(0, row(0.125)), (100.125, row(90.75)), (100.175, row(2))], end=600, pending=True
    )
    with ThreadPoolExecutor() as executor:
        request = executor.submit(
            capture.prepare_multi, {"a": raw_view(source, "a", "1970-01-01", 200)}, tmp_path
        ).result()
    parameters = {
        "device_id": "a",
        "date": "1970-01-01",
        "offset": 0,
        "limit": 2,
        "end": 200,
        "revision": 0,
    }
    request.update(operation="recording_rows", parameters=codec.encode(parameters))
    result = unpack_page(calculations.execute(request))
    assert result["total"] == 3
    assert result["next_offset"] == 2
    assert result["rows"][1][:3] == [100.125, 100.175, 90.75]
    parameters["offset"] = 2
    request["parameters"] = codec.encode(parameters)
    assert unpack_page(calculations.execute(request))["rows"][0][:3] == [100.175, 200, 2]
    path = tmp_path / "request.json"
    path.write_bytes(protocol.pack("learn", request))
    root = str(Path(calculations.__file__).parent.parent)
    script = "\n".join(
        [
            "import sys,types,pathlib",
            f"p=types.ModuleType('isolated_tuner');p.__path__=[{json.dumps(root)}]",
            "sys.modules[p.__name__]=p",
            "from isolated_tuner.worker import calculations,protocol,codec",
            "r=protocol.unpack(pathlib.Path(sys.argv[1]).read_bytes(),'learn')",
            "sys.stdout.buffer.write(codec.pack_result(calculations.execute(r)))",
            "assert 'homeassistant' not in sys.modules",
        ]
    )
    child = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", script, str(path)],
        capture_output=True,
        check=False,
    )
    assert child.returncode == 0, child.stderr.decode()
    assert codec.unpack_result(child.stdout) == calculations.execute(request)


def test_zero_duration_null_and_future_rows():
    from array import array

    from tuner_under_test.history import dayfile
    from tuner_under_test.worker.recordings import calculate

    day = dayfile.Day(
        0,
        86400,
        array("d", [0, 1, 3]),
        array("d", [0, 2, 3]),
        [bytearray([255, 4, 5]) for _ in range(18)],
        [],
    )
    result = calculate(
        SimpleNamespace(_days=SimpleNamespace(day=lambda *a, **kw: day)),
        "a",
        "1970-01-01",
        0,
        100,
        2,
        0,
    )
    assert result["rows"] == [[0, 0, *([None] * 18)], [1, 2, *([4] * 18)]]
    assert result["total"] == 2


def test_bootstrap_archived_and_recovery(tmp_path):
    from tuner_under_test.history.daystore import DayStore
    from tuner_under_test.presentation.recordings import recording_devices

    source = view([(0, row(3))], end=600, pending=True)
    source.data["devices"]["a"]["name"] = "Old radar"
    device = recording_devices(source)["devices"][0]
    assert device == {"id": "a", "name": "Old radar", "days": 1, "bytes": 0}
    recovery = tmp_path / ".ld2410-storage-interrupted" / "history" / "radar"
    recovery.mkdir(parents=True)
    (recovery / "sample.day").write_bytes(b"12345")
    source = SimpleNamespace(_days=DayStore(tmp_path / "history"), data={"devices": {}})
    assert recording_devices(source)["storage"] == {"history_bytes": 0, "recovery_bytes": 5}


def test_runtime_validation():
    from tuner_under_test.presentation.recordings import async_recording_rows

    source = view([(0, row(1))], end=10, pending=True)

    async def run():
        for parameters in (
            {"revision": 9},
            {"limit": 10001},
            {"offset": -1},
            {"end": float("nan")},
        ):
            with pytest.raises(ValueError):
                await async_recording_rows(source, "a", "1970-01-01", **parameters)

    asyncio.run(run())


def test_jobs_bounded_and_deduplicated():
    from tuner_under_test.presentation.recordings import _bounded
    from tuner_under_test.worker.supervisor import Busy

    async def run():
        runtime = SimpleNamespace(_recording_jobs={})
        gate = asyncio.Event()
        calls = []

        async def build():
            calls.append(1)
            await gate.wait()
            return 42

        first = asyncio.create_task(_bounded(runtime, "first", build))
        duplicate = asyncio.create_task(_bounded(runtime, "first", build))
        second = asyncio.create_task(_bounded(runtime, "second", build))
        await asyncio.sleep(0)
        with pytest.raises(Busy):
            await _bounded(runtime, "third", build)
        gate.set()
        assert await asyncio.gather(first, duplicate, second) == [42, 42, 42]
        assert len(calls) == 2
        assert runtime._recording_jobs == {}

    asyncio.run(run())


def test_runtime_uses_stable_observation_cutoff_and_real_websocket_routes(tmp_path, monkeypatch):
    from unittest.mock import Mock

    from test_tuner import WebsocketTests, mod
    from tuner_under_test.history import dayfile, daystore
    from tuner_under_test.runtime.coordinator import TunerRuntime
    from tuner_under_test.worker import bridge

    async def calculate(runtime, operation, prepare):
        from tuner_under_test.worker import calculations, capture, codec

        prepared = prepare()
        request = await asyncio.to_thread(
            capture.prepare_multi, prepared["views"], tmp_path / str(runtime._days.seen("a"))
        )
        request.update(operation=operation, parameters=codec.encode(prepared["parameters"]))
        return await asyncio.to_thread(calculations.execute, request)

    # Linux isolation is exercised by the lab; this boundary adapter keeps real capture/decode.
    monkeypatch.setattr(bridge, "calculate", calculate)

    async def run():
        harness = WebsocketTests()
        harness.setUp()
        hass = SimpleNamespace(async_add_executor_job=asyncio.to_thread)
        runtime = TunerRuntime(hass, None, {"devices": {"a": {"name": "Radar"}}})
        runtime._days = daystore.DayStore(tmp_path / "history")
        runtime._days.append("a", dayfile.Change(10.125, {0: 1.25}))
        runtime._days.observe("a", 100.875)
        assert runtime._days.last("a") == 10.125
        harness.hass.data[mod.DOMAIN] = runtime
        try:
            await harness.call("recording_days", device_id=None, offset=0, limit=50)
            inventory = harness.connection.send_result.call_args.args[1]
            assert inventory["devices"][0]["id"] == "a"
            await harness.call(
                "recording_rows",
                device_id="a",
                date="1970-01-01",
                offset=0,
                limit=100,
                end=None,
                revision=None,
            )
            assert not harness.connection.send_error.called
            result = harness.connection.send_result.call_args.args[1]
            assert result["end"] == 100.875
            assert result["rows"] == [[10.125, 100.875, 1.25, *([None] * 17)]]
            runtime._days.observe("a", 200.25)
            await harness.call(
                "recording_rows",
                device_id="a",
                date="1970-01-01",
                offset=0,
                limit=100,
                end=result["end"],
                revision=result["revision"],
            )
            assert harness.connection.send_result.call_args.args[1] == result
            harness.connection.send_error = Mock()
            runtime._days._rewrite("a")
            await harness.call(
                "recording_rows",
                device_id="a",
                date="1970-01-01",
                offset=0,
                limit=100,
                end=result["end"],
                revision=result["revision"],
            )
            assert harness.connection.send_error.call_args.args[1] == "invalid_request"
        finally:
            await bridge.stop(runtime)

    asyncio.run(run())


@pytest.mark.parametrize("corrupt", [False, True])
def test_finished_legacy_fractional_day_and_corrupt_file(tmp_path, corrupt):
    import datetime
    import struct
    import zlib

    from tuner_under_test.history import dayfile, daystore
    from tuner_under_test.presentation.recordings import raw_view
    from tuner_under_test.worker import calculations, capture, codec, history

    store = daystore.DayStore(tmp_path / "history")
    date = datetime.date(1970, 1, 1)
    path = store._path("a", date, ".day")
    path.parent.mkdir(parents=True)
    header = dayfile.HEADER.pack(1, 0, 86400000, 2, 0, 1, *([0] * 17))
    body = struct.pack("<4i", 1125, 50125, 9125, 60125)
    body += struct.pack("<2f", 1.25, float("nan")) + bytes([255, 4]) * 17
    path.write_bytes(b"corrupt" if corrupt else dayfile.MAGIC + zlib.compress(header + body))
    store.inventory("a", date, ".day", True)
    source = SimpleNamespace(_days=store)
    with ThreadPoolExecutor() as executor:
        request = executor.submit(
            capture.prepare_multi,
            {"a": raw_view(source, "a", str(date), 86400)},
            tmp_path / "capture",
        ).result()
    request.update(
        operation="recording_rows",
        parameters=codec.encode(
            {
                "device_id": "a",
                "date": str(date),
                "offset": 0,
                "limit": 1000,
                "end": 86400,
                "revision": 0,
            }
        ),
    )
    if corrupt:
        with pytest.raises(history.HistoryRejected):
            calculations.execute(request)
    else:
        result = unpack_page(calculations.execute(request))
        assert result["rows"] == [
            [1.125, 9.125, 1.25, *([None] * 17)],
            [50.125, 60.125, None, *([4] * 17)],
        ]


def test_thousand_row_page_fits_transport_and_empty_offset():
    from array import array

    from tuner_under_test.history import dayfile
    from tuner_under_test.worker import codec
    from tuner_under_test.worker.recordings import calculate

    day = dayfile.Day(
        0,
        86400,
        array("d", range(5000)),
        array("d", range(1, 5001)),
        [bytearray([7] * 5000) for _ in range(18)],
        [],
    )
    runtime = SimpleNamespace(_days=SimpleNamespace(day=lambda *a, **kw: day))
    result = calculate(runtime, "a", "1970-01-01", 2000, 1000, 4999, 0)
    assert len(result["rows"]) == 1000
    assert result["rows"][0][:2] == [2000, 2001]
    assert result["next_offset"] == 3000
    assert codec.unpack_result(codec.pack_result(result)) == result
    assert calculate(runtime, "a", "1970-01-01", 5000, 1000, 4999, 0)["rows"] == []


def test_zero_duration_row_at_utc_midnight_is_retained(tmp_path):
    from tuner_under_test.presentation.recordings import raw_view
    from tuner_under_test.worker import calculations, capture, codec

    source = view([(0, row(5))], end=0, pending=True)
    with ThreadPoolExecutor() as executor:
        request = executor.submit(
            capture.prepare_multi, {"a": raw_view(source, "a", "1970-01-01", 0)}, tmp_path
        ).result()
    request.update(
        operation="recording_rows",
        parameters=codec.encode(
            {
                "device_id": "a",
                "date": "1970-01-01",
                "offset": 0,
                "limit": 100,
                "end": 0,
                "revision": 0,
            }
        ),
    )
    assert unpack_page(calculations.execute(request))["rows"] == [[0, 0, *row(5)]]


def test_large_compressed_page_transport_is_exact_and_bounded():
    from tuner_under_test.worker import codec
    from tuner_under_test.worker.recordings import pack_page, unpack_page

    page = {
        "date": "1970-01-01",
        "keys": list(test_tuner.constants.HISTORY_KEYS),
        "rows": [[i / 1000, (i + 1) / 1000, 1.25, None, *([90.75] * 16)] for i in range(10000)],
        "total": 10000,
        "offset": 0,
        "next_offset": None,
        "end": 10,
        "revision": 0,
    }
    wire = pack_page(page)
    assert len(codec.pack_result(wire)) < 1024 * 1024
    assert unpack_page(codec.unpack_result(codec.pack_result(wire))) == page
    page["rows"].append(page["rows"][-1])
    with pytest.raises(ValueError):
        pack_page(page)


@pytest.mark.parametrize("value", [None, {}, {"recording_zlib": "!"}, {"recording_zlib": "AAAA"}])
def test_raw_page_transport_rejects_malformed(value):
    from tuner_under_test.worker.recordings import unpack_page

    with pytest.raises(ValueError):
        unpack_page(value)


def test_raw_bridge_does_not_traverse_unrelated_training_metadata(tmp_path, monkeypatch):
    from pathlib import Path

    from tuner_under_test.presentation.recordings import raw_view
    from tuner_under_test.worker import bridge, calculations, codec, protocol

    source = view([(0, row(1))], end=10, pending=True)
    source.hass = SimpleNamespace(async_add_executor_job=asyncio.to_thread)
    source.data["devices"]["unrelated"] = {"history_labels": [object()]}

    class Service:
        async def run_when_available(self, admitted, finish, background):
            try:
                path, *_ = await admitted()
                request = protocol.unpack(Path(path).read_bytes(), "learn")
                return await asyncio.to_thread(
                    lambda: codec.pack_result(calculations.execute(request))
                )
            finally:
                await finish()

    monkeypatch.setattr(bridge, "_service", lambda runtime, operation=None: Service())

    async def run():
        result = await bridge.calculate(
            source,
            "recording_rows",
            lambda: {
                "views": {"a": raw_view(source, "a", "1970-01-01", 10)},
                "parameters": {
                    "device_id": "a",
                    "date": "1970-01-01",
                    "offset": 0,
                    "limit": 100,
                    "end": 10,
                    "revision": 0,
                },
            },
        )
        assert unpack_page(result)["rows"][0][:3] == [0, 10, 1]

    asyncio.run(run())


def test_worker_corruption_becomes_safe_websocket_error(tmp_path, monkeypatch):
    from test_tuner import WebsocketTests, mod
    from tuner_under_test.history.daystore import DayStore
    from tuner_under_test.runtime.coordinator import TunerRuntime
    from tuner_under_test.worker import bridge
    from tuner_under_test.worker.supervisor import WorkerFailed

    async def refused(*args):
        raise WorkerFailed(1, b"HistoryRejected: private/sensor/location corrupted")

    monkeypatch.setattr(bridge, "calculate", refused)

    async def run():
        harness = WebsocketTests()
        harness.setUp()
        runtime = TunerRuntime(
            SimpleNamespace(async_add_executor_job=asyncio.to_thread), None, {"devices": {"a": {}}}
        )
        runtime._days = DayStore(tmp_path)
        harness.hass.data[mod.DOMAIN] = runtime
        await harness.call(
            "recording_rows",
            device_id="a",
            date="1970-01-01",
            offset=0,
            limit=100,
            end=1,
            revision=None,
        )
        args = harness.connection.send_error.call_args.args
        assert args[1] == "invalid_request"
        assert "could not be read" in args[2]
        assert "private" not in args[2]

    asyncio.run(run())


def test_large_fractional_pages_adapt_without_losing_rows():
    import random
    import struct

    from tuner_under_test.worker import codec
    from tuner_under_test.worker.recordings import pack_page, unpack_page

    rng = random.Random(42)
    rows = [
        [
            i / 1000,
            (i + 1) / 1000,
            *[struct.unpack("<f", struct.pack("<f", rng.random() * 100))[0] for _ in range(18)],
        ]
        for i in range(10000)
    ]
    restored = []
    offset = 0
    while offset < len(rows):
        page = {
            "date": "1970-01-01",
            "keys": list(test_tuner.constants.HISTORY_KEYS),
            "rows": rows[offset:],
            "total": len(rows),
            "offset": offset,
            "next_offset": None,
            "end": 10,
            "revision": 0,
        }
        encoded = codec.pack_result(pack_page(page))
        assert len(encoded) < 1024 * 1024
        decoded = unpack_page(codec.unpack_result(encoded))
        assert decoded["rows"]
        restored.extend(decoded["rows"])
        offset = decoded["next_offset"] or len(rows)
    assert restored == rows


@pytest.mark.parametrize("limit,background", [(100, False), (10000, True)])
def test_export_pages_yield_worker_admission(tmp_path, monkeypatch, limit, background):
    from tuner_under_test.history.daystore import DayStore
    from tuner_under_test.presentation.recordings import async_recording_rows
    from tuner_under_test.worker import bridge, priority
    from tuner_under_test.worker.recordings import pack_page

    async def calculate(runtime, operation, prepare):
        assert priority.is_background(operation) is background
        parameters = prepare()["parameters"].copy()
        parameters.pop("device_id")
        parameters.pop("limit")
        return pack_page(
            {
                **parameters,
                "keys": list(test_tuner.constants.HISTORY_KEYS),
                "rows": [],
                "total": 0,
                "next_offset": None,
            }
        )

    monkeypatch.setattr(bridge, "calculate", calculate)

    async def run():
        runtime = SimpleNamespace(
            data={"devices": {"a": {}}},
            _days=DayStore(tmp_path),
            _recording_jobs={},
            hass=SimpleNamespace(async_add_executor_job=asyncio.to_thread),
        )
        await async_recording_rows(runtime, "a", "1970-01-01", limit=limit, end=1)

    asyncio.run(run())
