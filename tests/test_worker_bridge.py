"""Production admission and shutdown reject work before expensive view construction."""

import asyncio
from types import SimpleNamespace

import pytest
import test_tuner  # noqa: F401
from tuner_under_test.worker import bridge, supervisor


def runtime():
    return SimpleNamespace(
        data={"devices": {"a": {}}},
        hass=SimpleNamespace(async_add_executor_job=lambda fn, *args: asyncio.to_thread(fn, *args)),
    )


def test_oversized_metadata_rejected_before_view_copy():
    async def run():
        owner = runtime()
        owner.data["devices"]["a"]["history_labels"] = [{"note": "x" * (2 * 1024 * 1024)}]

        def prepare():
            pytest.fail("copied unrestricted metadata")

        with pytest.raises(ValueError):
            await bridge.calculate(owner, "learn", prepare)
        await bridge.stop(owner)

    asyncio.run(run())


def test_shutdown_closes_admission_without_preparing():
    async def run():
        owner = runtime()
        await bridge.stop(owner)
        with pytest.raises(supervisor.Closed):
            await bridge.calculate(owner, "learn", lambda: pytest.fail("prepared after stop"))

    asyncio.run(run())


def test_preparer_failure_is_released_and_next_request_can_enter():
    async def run():
        owner = runtime()
        calls = []

        def prepare():
            calls.append(True)
            raise ValueError("view rejected")

        for _ in range(2):
            with pytest.raises(ValueError, match="view rejected"):
                await bridge.calculate(owner, "learn", prepare)
        assert calls == [True, True]
        await bridge.stop(owner)

    asyncio.run(run())


def test_reference_profile_budget_precedes_reference_view_copy():
    owner = runtime()
    owner.data["devices"]["a"]["reference_profile"] = {"periods": ["x" * (2 * 1024 * 1024)]}
    with pytest.raises(ValueError):
        bridge.preflight(owner)


def test_seen_by_defers_view_copy_until_admission(monkeypatch):
    from tuner_under_test.rooms import seen_by

    async def run():
        semaphore = asyncio.Semaphore(0)
        owner = runtime()
        owner._room_attribution_jobs = {}
        owner._room_attribution_semaphore = semaphore
        owner.hass.async_create_task = asyncio.create_task
        monkeypatch.setattr(seen_by, "_view", lambda *args: pytest.fail("copied before admission"))
        monkeypatch.setattr(seen_by, "_has_recording", lambda *args: True)
        seen_by._refresh(owner, {"room": {"device_ids": ["a"]}}, "room", {}, 0)
        task = owner._room_attribution_jobs["room"]
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await bridge.stop(owner)

    asyncio.run(run())
