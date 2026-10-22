"""Room charts share one display job and the single-radar cache."""

import asyncio
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_tuner  # noqa: F401
from test_event_charts import row, view
from tuner_under_test.history import dayfile
from tuner_under_test.presentation import charts
from tuner_under_test.worker import bridge, calculations, codec, display, protocol


def sources():
    result = {}
    for index, device_id in enumerate(("a", "b", "c")):
        source = view([(0, row(10 + index))], end=86400)
        if device_id != "a":
            source.data["devices"][device_id] = source.data["devices"].pop("a")
            source._days.append(device_id, dayfile.Change(0, {0: 20 + index}))
            source._days.append(device_id, dayfile.End(86400))
        result[device_id] = source
    return result


def parameters(ids):
    return {"device_ids": ids, "keys": ["g0_move"], "hours": 24, "max_points": 400, "end": 86400}


def test_room_charts_real_manifest_and_result_transport():
    import shutil

    views = sources()
    views.pop("c")
    holder = {}
    with ThreadPoolExecutor() as executor:
        path, *_ = executor.submit(
            bridge._build, holder, "charts", {"views": views, "parameters": parameters(list(views))}
        ).result()
    try:
        packed = calculations.execute(protocol.unpack(Path(path).read_bytes(), "learn"))
        result = display.unpack_charts(codec.unpack_result(codec.pack_result(packed)), list(views))
        assert result["errors"] == {}
        for device_id, source in views.items():
            assert result["charts"][device_id] == charts.history_series_multi(
                source, device_id, ["g0_move"], 24, 400, 86400
            )
    finally:
        shutil.rmtree(holder["directory"])


def test_worker_room_keeps_other_charts_on_value_error():
    views = sources()
    views["b"].data["devices"].clear()
    result = display.unpack_charts(
        display.calculate_charts(views, parameters(["a", "b"])), ["a", "b"]
    )
    assert set(result["charts"]) == {"a"}
    assert result["errors"] == {"b": "Unknown device"}


@pytest.mark.parametrize(
    "value",
    [
        {"charts": {}, "errors": {}, "extra": 1},
        {"charts": {"x": {}}, "errors": {}},
        {"charts": {"a": {}}, "errors": {"a": "bad"}},
        {"charts": {}, "errors": {"a": 1}},
        {"charts": {}, "errors": {"a": "x" * 501}},
        {"charts": {"a": {}}, "errors": {}},
        {"charts": [], "errors": {}},
        {"charts": {}, "errors": {1: "bad"}},
        None,
    ],
)
def test_unpack_room_rejects_invalid_envelopes(value):
    with pytest.raises(ValueError):
        display.unpack_charts(value, ["a"])


def runtime():
    views = sources()
    return SimpleNamespace(
        data={"devices": {key: source.data["devices"][key] for key, source in views.items()}},
        _history_cache=OrderedDict(),
        _history_jobs={},
        _history_view=lambda device_id, **kwargs: views[device_id],
        hass=SimpleNamespace(async_add_executor_job=asyncio.to_thread),
    )


def test_room_one_job_cache_partial_and_unknown(monkeypatch):
    calls = []

    async def calculate(owner, operation, prepare):
        prepared = prepare()
        calls.append(prepared)
        assert operation == "charts"
        for source in prepared["views"].values():
            assert source._capture_chart_window
            assert source._snapshot_history_start == 0
            assert source._history_until == 86400
        return display.calculate_charts(prepared["views"], prepared["parameters"])

    monkeypatch.setattr(bridge, "calculate", calculate)

    async def run():
        owner = runtime()
        result = await charts.async_room_series(owner, **parameters(["a", "b", "c"]))
        assert set(result["charts"]) == {"a", "b", "c"}
        assert len(calls) == 1
        assert await charts.async_room_series(owner, **parameters(["a", "b", "c"])) == result
        assert (
            await charts.async_history_series(owner, "b", ["g0_move"], 24, 400, 86400)
            == result["charts"]["b"]
        )
        assert len(calls) == 1
        owner = runtime()
        await charts.async_room_series(owner, **parameters(["a"]))
        calls.clear()
        result = await charts.async_room_series(owner, **parameters(["a", "b", "x"]))
        assert result["errors"] == {"x": "Unknown device"}
        assert set(result["charts"]) == {"a", "b"}
        assert len(calls) == 1
        assert set(calls[0]["views"]) == {"b"}
        assert calls[0]["parameters"]["device_ids"] == ["b"]

    asyncio.run(run())


@pytest.mark.parametrize(
    "ids,end,keys,hours",
    [
        ([], 86400, ["g0_move"], 24),
        (["a", "a"], 86400, ["g0_move"], 24),
        (list("abcde"), 86400, ["g0_move"], 24),  # Five worst-case charts exceed the result cap.
        ([1], 86400, ["g0_move"], 24),
        (("a",), 86400, ["g0_move"], 24),
        (["a"], float("nan"), ["g0_move"], 24),
        (["a"], float("inf"), ["g0_move"], 24),
        (["x"], 86400, ["invalid"], 24),
        (["x"], 86400, ["g0_move"], float("inf")),
    ],
)
def test_room_request_validation(ids, end, keys, hours):
    with pytest.raises(ValueError):
        asyncio.run(charts.async_room_series(runtime(), ids, keys, hours, end=end))


def test_room_websocket_route(monkeypatch):
    from tuner_under_test.runtime import websocket
    from tuner_under_test.runtime.coordinator import TunerRuntime

    monkeypatch.setattr(websocket.vol, "Required", lambda name: name, raising=False)
    monkeypatch.setattr(
        websocket.vol, "Optional", lambda name, **kw: (name, kw.get("default")), raising=False
    )
    monkeypatch.setattr(websocket.vol, "Coerce", lambda kind: kind, raising=False)
    for name in ("In", "Any", "All", "Range"):
        monkeypatch.setattr(websocket.vol, name, lambda *args, **kwargs: None, raising=False)
    route = next(route for route in websocket._websocket_routes() if route[0] == "room_series")
    assert route[1] == {
        "device_ids": [str],
        "keys": [str],
        ("hours", 24): float,
        ("max_points", 400): int,
        ("end", None): float,
    }
    assert route[2:] == (
        "async_room_series",
        ("device_ids", "keys", "hours", "max_points", "end"),
        "invalid_request",
        False,
    )
    assert TunerRuntime.async_room_series is charts.async_room_series


def test_room_job_sharing_cancellation_and_admission(monkeypatch):
    from tuner_under_test.worker.supervisor import Busy

    async def run():
        owner = runtime()
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def calculate(runtime, operation, prepare):
            prepared = prepare()
            calls.append(prepared)
            entered.set()
            await release.wait()
            return display.calculate_charts(prepared["views"], prepared["parameters"])

        monkeypatch.setattr(bridge, "calculate", calculate)
        first = asyncio.create_task(charts.async_room_series(owner, **parameters(["a", "b"])))
        await entered.wait()
        shared = asyncio.create_task(charts.async_room_series(owner, **parameters(["a", "b"])))
        await asyncio.sleep(0)
        assert len(owner._history_jobs) == 1
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        other = asyncio.create_task(charts.async_room_series(owner, **parameters(["c"])))
        await asyncio.sleep(0)
        with pytest.raises(Busy):
            await charts.async_room_series(owner, ["a"], ["g0_move"], 1, end=12345)
        release.set()
        assert set((await shared)["charts"]) == {"a", "b"}
        assert set((await other)["charts"]) == {"c"}
        assert len(calls) == 2
        assert owner._history_jobs == {}

    asyncio.run(run())


def test_room_job_failure_is_removed(monkeypatch):
    async def calculate(*args):
        raise ValueError("transport rejected")

    monkeypatch.setattr(bridge, "calculate", calculate)

    async def run():
        owner = runtime()
        with pytest.raises(ValueError, match="transport rejected"):
            await charts.async_room_series(owner, **parameters(["a"]))
        assert owner._history_jobs == {}
        assert owner._history_cache == {}

    asyncio.run(run())
