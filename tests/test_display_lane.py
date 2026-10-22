"""Read-only display jobs have their own single worker lane beside numerical work."""

import asyncio
import importlib
import io
import sys
from types import SimpleNamespace

import pytest
import test_tuner  # noqa: F401
from tuner_under_test.worker import bridge, codec, supervisor

MIB = 1024 * 1024


def owner():
    return SimpleNamespace(
        data={"devices": {}},
        hass=SimpleNamespace(async_add_executor_job=asyncio.to_thread),
    )


@pytest.fixture
def children(monkeypatch):
    """Fake children: ``learn`` holds its worker until released; others finish at once."""
    state = SimpleNamespace(running=[], peak={}, release=None)

    async def process(self, args):
        operation = args[0]
        state.running.append(operation)
        state.peak[operation] = max(state.peak.get(operation, 0), state.running.count(operation))
        try:
            if operation == "learn":
                await state.release.wait()
            else:
                await asyncio.sleep(0.01)
            return codec.pack_result({"operation": operation})
        finally:
            state.running.remove(operation)

    monkeypatch.setattr(supervisor.Supervisor, "_process", process)
    monkeypatch.setattr(bridge, "_build", lambda holder, operation, prepared: (operation,))
    monkeypatch.setattr(bridge, "preflight", lambda runtime: None)
    return state


def test_chart_completes_while_learning_holds_the_numerical_worker(children):
    async def run():
        children.release = asyncio.Event()
        runtime = owner()
        learning = asyncio.create_task(bridge.calculate(runtime, "learn", dict))
        while "learn" not in children.running:
            await asyncio.sleep(0)
        chart = await asyncio.wait_for(bridge.calculate(runtime, "charts", dict), 5)
        assert chart == {"operation": "charts"}
        assert not learning.done()
        children.release.set()
        assert await learning == {"operation": "learn"}
        await bridge.stop(runtime)

    asyncio.run(run())


def test_display_jobs_still_run_one_child_at_a_time(children):
    async def run():
        runtime = owner()
        operations = ["charts", "charts", "export_days", "recording_rows"] * 2
        results = await asyncio.gather(
            *(bridge.calculate(runtime, operation, dict) for operation in operations)
        )
        assert [item["operation"] for item in results] == operations
        assert sum(children.peak.values()) <= len(children.peak)  # Never two at once.
        assert all(peak == 1 for peak in children.peak.values())
        await bridge.stop(runtime)

    asyncio.run(run())


def test_numerical_work_is_not_moved_to_the_display_lane(children):
    async def run():
        children.release = asyncio.Event()
        runtime = owner()
        first = asyncio.create_task(bridge.calculate(runtime, "learn", dict))
        while "learn" not in children.running:
            await asyncio.sleep(0)
        queued = asyncio.create_task(bridge.calculate(runtime, "references", dict))
        await asyncio.sleep(0.05)
        assert not queued.done()  # Numerical jobs keep a single shared turn.
        children.release.set()
        await asyncio.gather(first, queued)
        await bridge.stop(runtime)

    asyncio.run(run())


def test_stop_closes_and_reaps_the_display_lane(children):
    async def run():
        runtime = owner()
        await bridge.calculate(runtime, "charts", dict)
        await bridge.stop(runtime)
        with pytest.raises(supervisor.Closed):
            await bridge.calculate(runtime, "charts", dict)
        assert not runtime._display_service._jobs

    asyncio.run(run())


@pytest.mark.parametrize(
    ("extra", "memory"), [([], 512 * MIB), (["display"], 256 * MIB), (["storage"], 512 * MIB)]
)
def test_display_child_installs_a_smaller_memory_budget(tmp_path, monkeypatch, extra, memory):
    from tuner_under_test.worker import entrypoint, protocol

    path = tmp_path / "request.json"
    path.write_bytes(protocol.pack("learn", {"operation": "unsupported"}))
    entrypoint._namespace()
    limits = importlib.import_module("ld2410_child.worker.limits")
    installed = []
    monkeypatch.setattr(limits, "install", lambda budget, **kwargs: installed.append(budget) or {})
    monkeypatch.setattr(sys, "argv", ["entrypoint.py", str(path), "123", *extra])
    monkeypatch.setattr(sys, "stdout", SimpleNamespace(buffer=io.BytesIO()))
    with pytest.raises((ValueError, KeyError)):
        entrypoint.main()
    assert installed[0].memory_bytes == memory


def test_display_requests_are_marked_for_the_display_child(tmp_path, monkeypatch):
    import tempfile

    from tuner_under_test.worker import capture

    monkeypatch.setattr(tempfile, "mkdtemp", lambda prefix: str(tmp_path))
    monkeypatch.setattr(capture, "prepare_multi", lambda views, directory, sink: None)
    prepared = {"views": {}, "parameters": {}}
    assert bridge._build({}, "charts", prepared)[2:] == ("display",)
    (tmp_path / "request.json").unlink()
    (tmp_path / "history.sqlite").unlink(missing_ok=True)
    assert bridge._build({}, "learn", prepared)[2:] == ()
