"""Chart refreshes wait within a fixed budget without copying queued history."""

import asyncio
import gc
from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import test_tuner  # noqa: F401
from tuner_under_test.presentation import charts
from tuner_under_test.worker import bridge, codec, supervisor

# An empty chart as the worker returns it (expansion is stubbed out below).
CHART = {"start": 0, "end": 1, "bucket_seconds": 30, "series": {}, "labels": []}


def test_saturated_chart_refresh_is_bounded_and_deduplicated(monkeypatch):
    from tuner_under_test.worker import display

    # This test isolates queue admission; transport geometry is covered separately.
    monkeypatch.setattr(display, "unpack_chart", lambda value: value)

    async def run():
        copied = []
        owner = SimpleNamespace(
            data={"devices": {"a": {"label_revision": 1}}},
            _history_cache=OrderedDict(),
            _history_jobs={},
            _history_view=lambda *args, **kwargs: copied.append(True) or SimpleNamespace(),
            hass=SimpleNamespace(async_add_executor_job=asyncio.to_thread),
        )
        service = bridge._service(owner, "charts")
        entered, release = asyncio.Event(), asyncio.Event()

        async def process(args):
            entered.set()
            await release.wait()
            return codec.pack_result(CHART)

        async def prepare():
            return ()

        monkeypatch.setattr(service, "_process", process)
        monkeypatch.setattr(bridge, "_build", lambda *args: ())
        release.set()
        cached = await charts.async_history_series(owner, "a", ["g0_move"], 1, 100, 1100)
        release.clear()
        entered.clear()
        copied.clear()
        active = asyncio.create_task(service.run(prepare))
        await entered.wait()
        pending = asyncio.create_task(service.run(prepare))
        await asyncio.sleep(0)
        requests = [
            asyncio.create_task(charts.async_history_series(owner, "a", ["g0_move"], 1, 100, end))
            for end in (1200, 1201, 1200)
        ]
        for _ in range(5):
            await asyncio.sleep(0)
        assert all(not task.done() for task in requests)
        assert copied == []
        assert len(service._jobs) == 2
        assert await charts.async_history_series(owner, "a", ["g0_move"], 1, 100, 1100) == cached
        with pytest.raises(supervisor.Busy):
            await charts.async_history_series(owner, "a", ["g0_move"], 1, 100, 1202)
        assert len(owner._history_jobs) == 2
        requests[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await requests[0]
        release.set()
        assert await asyncio.gather(*requests[1:]) == [CHART, CHART]
        await asyncio.gather(active, pending)
        assert copied == [True, True]
        assert owner._history_jobs == {}
        assert await charts.async_history_series(owner, "a", ["g0_move"], 1, 100, 1200) == CHART
        assert copied == [True, True]
        await bridge.stop(owner)

    asyncio.run(run())


def test_shutdown_releases_abandoned_chart_waiter_without_task_error(monkeypatch):
    async def run():
        errors, copied = [], []
        asyncio.get_running_loop().set_exception_handler(
            lambda loop, context: errors.append(context)
        )
        owner = SimpleNamespace(
            data={"devices": {"a": {"label_revision": 1}}},
            _history_cache=OrderedDict(),
            _history_jobs={},
            _history_view=lambda *args, **kwargs: copied.append(True),
            hass=SimpleNamespace(async_add_executor_job=asyncio.to_thread),
        )
        service = bridge._service(owner, "charts")
        entered = asyncio.Event()

        async def process(args):
            entered.set()
            await asyncio.Event().wait()

        async def prepare():
            return ()

        monkeypatch.setattr(service, "_process", process)
        active = asyncio.create_task(service.run(prepare))
        await entered.wait()
        pending = asyncio.create_task(service.run(prepare))
        await asyncio.sleep(0)
        request = asyncio.create_task(
            charts.async_history_series(owner, "a", ["g0_move"], 1, 100, 1200)
        )
        for _ in range(4):
            await asyncio.sleep(0)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        await bridge.stop(owner)
        assert owner._history_jobs == {}
        await asyncio.gather(active, pending, return_exceptions=True)
        for _ in range(6):
            await asyncio.sleep(0)
        gc.collect()
        assert owner._history_jobs == {}
        assert owner._history_cache == {}
        assert service._jobs == set()
        assert copied == []
        assert errors == []

    asyncio.run(run())


@pytest.mark.parametrize(
    "error,code", [(supervisor.Busy, "busy"), (supervisor.Closed, "unavailable")]
)
def test_websocket_worker_contention_has_structured_response(monkeypatch, error, code):
    from tuner_under_test.runtime import websocket

    async def run():
        async def calculate():
            raise error("internal worker detail")

        monkeypatch.setattr(websocket.vol, "Required", lambda value: value, raising=False)
        monkeypatch.setattr(websocket.websocket_api, "async_response", lambda fn: fn, raising=False)
        monkeypatch.setattr(
            websocket.websocket_api,
            "websocket_command",
            lambda schema: lambda fn: fn,
            raising=False,
        )
        monkeypatch.setattr(websocket.websocket_api, "require_admin", lambda fn: fn, raising=False)
        owner = SimpleNamespace(calculate=calculate)
        hass = SimpleNamespace(data={websocket.DOMAIN: owner})
        connection = SimpleNamespace(send_error=Mock(), send_result=Mock())
        handle = websocket._websocket_handler(("chart", {}, "calculate", (), None, False))
        await handle(hass, connection, {"id": 1})
        assert connection.send_error.call_args.args[:2] == (1, code)
        assert "internal worker detail" not in connection.send_error.call_args.args[2]
        connection.send_result.assert_not_called()

    asyncio.run(run())
