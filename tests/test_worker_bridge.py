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
        owner.data["devices"]["a"]["history_labels"] = [
            {"note": "x" * (bridge.PREFLIGHT_BYTES + 1)}
        ]

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
    owner.data["devices"]["a"]["reference_profile"] = {
        "periods": ["x" * (bridge.PREFLIGHT_BYTES + 1)]
    }
    with pytest.raises(ValueError):
        bridge.preflight(owner)


def test_preflight_accepts_a_large_home_with_months_of_bermuda_labels():
    owner = runtime()
    # A real radar's learning counts are about 3,700 values; Bermuda adds about 250 labels a day, 30 days of that here.
    histograms = {
        f"g{gate}": {"present": [0] * 100, "not_present": [0] * 100} for gate in range(18)
    }
    owner.data["devices"] = {f"r{i}": {"histograms": histograms} for i in range(28)}
    label = {"start": 1.0, "end": 2.0, "state": "present", "source": "sensor", "confidence": 1.0}
    owner.data["room_labels"] = {f"area:{i}": [label] * 536 for i in range(14)}
    bridge.preflight(owner)


def test_preflight_still_refuses_unbounded_metadata():
    owner = runtime()
    owner.data["room_labels"] = {"area:a": [{"start": 1.0}] * (bridge.METADATA_NODES + 1)}
    with pytest.raises(ValueError, match="budget exceeded"):
        bridge.preflight(owner)


def test_metadata_check_stays_off_the_home_assistant_loop(monkeypatch):
    import threading

    threads = []
    original = bridge.preflight

    def checked(owner):
        threads.append(threading.current_thread())
        return original(owner)

    async def run():
        owner = runtime()
        monkeypatch.setattr(bridge, "preflight", checked)

        def prepare():
            raise ValueError("stop here")

        with pytest.raises(ValueError, match="stop here"):
            await bridge.calculate(owner, "learn", prepare)
        await bridge.stop(owner)

    asyncio.run(run())
    assert threads and threads[0] is not threading.main_thread()


def test_metadata_check_retries_when_the_loop_changes_data_underneath_it(monkeypatch):
    attempts = []

    def changing(owner):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("dictionary changed size during iteration")

    async def run():
        owner = runtime()
        monkeypatch.setattr(bridge, "preflight", changing)
        await bridge._checked(owner)

    asyncio.run(run())
    assert len(attempts) == 2


def test_metadata_check_finishes_on_the_loop_if_data_keeps_changing(monkeypatch):
    import threading

    threads = []

    def changing(owner):
        threads.append(threading.current_thread())
        if threads[-1] is not threading.main_thread():
            raise RuntimeError("dictionary changed size during iteration")

    async def run():
        monkeypatch.setattr(bridge, "preflight", changing)
        await bridge._checked(runtime())

    asyncio.run(run())
    assert threads[-1] is threading.main_thread()


def test_a_long_fit_cannot_hold_up_saving_recordings():
    async def run():
        owner = runtime()
        fit = bridge._service(owner, "learn")
        storage = bridge._storage_service(owner)
        assert storage is not fit
        assert storage is not bridge._service(owner, "charts")
        assert fit.deadline >= 3600
        assert storage.deadline == 180
        assert bridge._service(owner, "charts").deadline == 180
        await bridge.stop(owner)
        assert storage.diagnostics()["closed"]

    asyncio.run(run())


def test_only_flushes_use_the_storage_lane(monkeypatch):
    from tuner_under_test.worker import transactions

    used = []

    async def admit(service, prepare, consume, finish, operation):
        assert operation in {"flush", "maintenance"}
        used.append(service)

    async def run():
        owner = runtime()
        monkeypatch.setattr(transactions, "_admit", admit)
        await transactions.run(owner, "flush", {}, None)
        assert used == [bridge._storage_service(owner)]
        # Rewrites stay in line with label recounts and fits: they would race otherwise.
        await transactions.run(owner, "maintenance", {}, None)
        assert used[1] is bridge._service(owner, "learn")
        await bridge.stop(owner)

    asyncio.run(run())


def test_fit_children_ask_for_idle_disk_priority(monkeypatch):
    from tuner_under_test.worker import limits

    calls = []
    libc = SimpleNamespace(syscall=lambda *args: calls.append(args) or 0)
    monkeypatch.setattr(limits.platform, "machine", lambda: "aarch64")
    limits.idle_disk_priority(libc)
    assert calls == [(30, 1, 0, 3 << 13)]


def test_unknown_machines_skip_idle_disk_priority(monkeypatch):
    from tuner_under_test.worker import limits

    calls = []
    libc = SimpleNamespace(syscall=lambda *args: calls.append(args) or 0)
    monkeypatch.setattr(limits.platform, "machine", lambda: "riscv64")
    limits.idle_disk_priority(libc)
    assert not calls


def test_job_manifest_pages_day_metadata_without_changing_the_views(tmp_path):
    import datetime

    from tuner_under_test.worker.calculations import restore
    from tuner_under_test.worker.manifest import Writer, expand

    path = tmp_path / "history.sqlite"
    start = datetime.date(2025, 1, 1)
    days = [
        {
            "device_id": "a",
            "date": str(start + datetime.timedelta(days=i)),
            "path": None,
            "identity": None,
            "late": [],
        }
        for i in range(6000)
    ]
    request = {
        "data": {"devices": {"a": {}}},
        "attributes": {},
        "days": days,
        "budgets": {},
        "live_dates": {},
    }
    with Writer(path) as writer:
        writer("view", request)
    expanded = expand({"manifest": str(path), "operation": "charts", "parameters": {}})
    assert list(expanded["views"]) == ["view"]
    restored = restore(expanded["views"]["view"])
    assert restored.data == request["data"]
    assert restored._days.days("a") == [start + datetime.timedelta(days=i) for i in range(6000)]
    assert restored._days.entries[("a", start)] == days[0]
    assert restored._days.days("unknown") == []
    # Executor jobs read from other threads, and the owner removes the job
    # directory while results may still reference these lazy views.
    assert asyncio.run(asyncio.to_thread(len, restored._days.entries)) == 6000
    path.unlink()


def test_seen_by_defers_view_copy_until_admission(monkeypatch):
    from tuner_under_test.rooms import seen_by

    async def run():
        semaphore = asyncio.Semaphore(0)
        owner = runtime()
        owner._room_attribution_jobs = {}
        owner._room_attribution_semaphore = semaphore
        owner.hass.async_create_background_task = lambda target, name: asyncio.create_task(target)
        monkeypatch.setattr(seen_by, "_view", lambda *args: pytest.fail("copied before admission"))
        monkeypatch.setattr(seen_by, "_has_recording", lambda *args: True)
        seen_by._refresh(owner, {"room": {"device_ids": ["a"]}}, "room", {}, 0)
        task = owner._room_attribution_jobs["room"]
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await bridge.stop(owner)

    asyncio.run(run())


@pytest.mark.parametrize("operation", ["seen_by", "references", "learn"])
def test_background_calculation_waits_for_saturated_worker_without_copying(operation, monkeypatch):
    from tuner_under_test.worker import codec

    async def run():
        owner = runtime()
        service = bridge._service(owner)
        entered, release = asyncio.Event(), asyncio.Event()
        prepared = []

        async def process(args):
            entered.set()
            await release.wait()
            return codec.pack_result({"episodes": [1]})

        async def prepare_slot():
            return ()

        monkeypatch.setattr(service, "_process", process)
        monkeypatch.setattr(bridge, "_build", lambda *args: ())
        active = asyncio.create_task(service.run(prepare_slot))
        await entered.wait()
        pending = asyncio.create_task(service.run(prepare_slot))
        await asyncio.sleep(0)

        def prepare():
            prepared.append(True)
            return {}

        waiting = asyncio.create_task(bridge.calculate(owner, operation, prepare))
        await asyncio.sleep(0)
        assert not waiting.done()
        assert prepared == []
        release.set()
        assert await waiting == {"episodes": [1]}
        await asyncio.gather(active, pending)
        assert prepared == [True]
        await bridge.stop(owner)

    asyncio.run(run())


def test_cancelling_background_admission_keeps_active_work_owned(monkeypatch):
    async def run():
        owner = runtime()
        service = bridge._service(owner)
        entered, release = asyncio.Event(), asyncio.Event()

        async def process(args):
            entered.set()
            await release.wait()
            return b"done"

        async def prepare():
            return ()

        monkeypatch.setattr(service, "_process", process)
        active = asyncio.create_task(service.run(prepare))
        await entered.wait()
        pending = asyncio.create_task(service.run(prepare))
        await asyncio.sleep(0)
        learning = asyncio.create_task(
            bridge.calculate(owner, "learn", lambda: pytest.fail("copied while waiting"))
        )
        await asyncio.sleep(0)
        assert not learning.done()
        learning.cancel()
        with pytest.raises(asyncio.CancelledError):
            await learning
        waiting = asyncio.create_task(
            bridge.calculate(owner, "seen_by", lambda: pytest.fail("copied while waiting"))
        )
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert not active.done()
        release.set()
        assert await active == b"done"
        assert await pending == b"done"
        await bridge.stop(owner)

    asyncio.run(run())
