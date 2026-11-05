"""Synthetic maintenance admission and durable bounded retention."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import test_tuner
from tuner_under_test.history import budget, dayfile, daystore, policy, recording, storage
from tuner_under_test.worker import transactions

NOW = 2_000_000_000


def runtime_at(root):
    runtime = test_tuner.mod.TunerRuntime(
        SimpleNamespace(async_add_executor_job=asyncio.to_thread),
        SimpleNamespace(async_save=AsyncMock()),
        {"devices": {"a": {"history_labels": []}}},
    )
    runtime._days = daystore.DayStore(root)
    return runtime


def test_routine_save_checks_size_off_loop_without_worker_or_copy(tmp_path, monkeypatch):
    async def run():
        runtime = runtime_at(tmp_path / "history")
        main = threading.get_ident()
        original = runtime._days.size
        checks = []

        def size():
            checks.append(threading.get_ident())
            assert runtime._storage_lock.locked()
            return original()

        async def forbidden(*args, **kwargs):
            pytest.fail("routine save started a worker")

        monkeypatch.setattr(runtime._days, "size", size)
        monkeypatch.setattr(transactions, "run", forbidden)
        await storage.async_save(runtime)
        assert checks and main not in checks
        runtime.store.async_save.assert_awaited_once()
        assert runtime.store.async_save.call_args.args[0] == runtime.data

    asyncio.run(run())


def test_automatic_failures_back_off_durably_and_trim_bypasses(tmp_path, monkeypatch):
    async def run():
        runtime = runtime_at(tmp_path / "history")
        attempts = []

        async def fail(*args, **kwargs):
            attempts.append(1)
            raise ValueError("CPU budget exceeded")

        monkeypatch.setattr(storage, "_save_days", fail)
        monkeypatch.setattr(recording.time, "time", lambda: NOW)
        for expected in (policy.DAY, 2 * policy.DAY, 4 * policy.DAY, 7 * policy.DAY):
            with pytest.raises(ValueError, match="CPU budget"):
                await recording.async_clean_history(runtime)
            state = runtime.data["maintenance_retry"]
            assert state["after"] == NOW + expected
            assert not recording._cleanup_due(runtime, NOW + expected - 1)
            assert recording._cleanup_due(runtime, NOW + expected)
            runtime.store.async_save.assert_awaited()
        with pytest.raises(ValueError, match="CPU budget"):
            await storage.trim(runtime)
        assert len(attempts) == 5

    asyncio.run(run())


def test_batched_retention_converges_to_unbounded_retained_days(tmp_path):
    from tuner_under_test.worker import retention

    store = daystore.DayStore(tmp_path / "source")
    data = {"devices": {key: {"history_labels": []} for key in ("a", "b")}}
    for key in data["devices"]:
        for age in (2, 8, 20, 40, 50):
            date = daystore.date_of(NOW - age * policy.DAY)
            low, high = daystore.bounds(date)
            builder = dayfile.DayBuilder(low, high)
            builder.add(dayfile.Change(low + 1, {0: 25}))
            builder.add(dayfile.End(high - 1))
            store.replace(key, date, builder.day())
        data["devices"][key]["history_labels"] = [
            {"start": NOW - 45 * policy.DAY, "end": NOW, "state": "present"}
        ]
    expected, changes, _ = budget.prepare_days(store, data, NOW, clean=True)
    retained = {(key, date): day for key, date, day in changes}
    for key in data["devices"]:
        for date in store.days(key):
            retained.setdefault((key, date), store.day(key, date))
    passes = 0
    while True:
        batch = retention.snapshot(store, data, NOW)
        directory = tmp_path / f"pass-{passes}"
        transactions.copy_tree(store.root, directory / "history", batch.pop("_source_paths"))
        request = {
            "root": directory / "history",
            "directory": directory,
            "data": data,
            "now": NOW,
            **batch,
        }
        result = retention.execute(request)
        prepared, actual, _, staged = __import__(
            "tuner_under_test.worker.maintenance", fromlist=["load"]
        ).load(result, directory)
        assert len(actual) <= retention.BATCH_DAYS
        store.commit(staged)
        data = prepared
        # Reopen each time: progress is durable, not tied to worker memory.
        store = daystore.DayStore(store.root)
        store.open(NOW)
        passes += 1
        if not data.get("maintenance_progress"):
            break
        assert passes < 20
    assert passes > 1
    for (key, date), day in retained.items():
        actual = store.day(key, date)
        assert actual == day if len(day.times) or day.guesses else actual is None
    for key in expected["devices"]:
        assert data["devices"][key]["histograms"] == expected["devices"][key]["histograms"]
        assert data["devices"][key]["history_labels"] == expected["devices"][key]["history_labels"]


def test_failed_worker_keeps_published_batch_and_resume_cursor(tmp_path, monkeypatch):
    from copy import deepcopy

    from tuner_under_test.worker import retention
    from tuner_under_test.worker.supervisor import Supervisor, WorkerFailed
    from worker_transport import Transport

    monkeypatch.setattr(retention, "BATCH_DAYS", 4)

    async def run():
        runtime = runtime_at(tmp_path / "history")
        runtime._days.root.mkdir()
        (runtime._days.root / "unrelated.index").write_bytes(b"x" * (8 * policy.MIB))
        for age in range(10, 20):
            date = daystore.date_of(NOW - age * policy.DAY)
            low, high = daystore.bounds(date)
            builder = dayfile.DayBuilder(low, high)
            builder.add(dayfile.Change(low + 1, {0: 25}))
            builder.add(dayfile.End(high - 1))
            runtime._days.replace("a", date, builder.day())
        transport = Transport()

        async def process(service, args):
            return await transport.process(args)

        monkeypatch.setattr(Supervisor, "_process", process)
        monkeypatch.setattr("time.time", lambda: NOW)
        await recording.async_clean_history(runtime)
        first = deepcopy(runtime.data["maintenance_progress"])
        saved = deepcopy(runtime.store.async_save.call_args.args[0])
        assert saved["maintenance_progress"] == first
        assert len(runtime._days.days("a")) == 6
        assert len(transport.requests[0]["batch"]["selected"]) == 4
        assert not list(
            __import__("pathlib").Path(transport.requests[0]["root"]).glob("unrelated*")
        )

        async def fail(service, args):
            raise WorkerFailed(1, b"CPU time budget exhausted")

        monkeypatch.setattr(Supervisor, "_process", fail)
        with pytest.raises(WorkerFailed):
            await recording.async_clean_history(runtime)
        assert runtime.data["maintenance_progress"] == first
        assert len(runtime._days.days("a")) == 6
        assert not recording._cleanup_due(runtime, NOW + policy.DAY - 1)
        monkeypatch.setattr(Supervisor, "_process", process)
        await recording.async_clean_history(
            runtime
        )  # Explicit invocation can continue immediately.
        assert runtime.data["maintenance_progress"]["after"] != first["after"]
        assert len(runtime._days.days("a")) == 2

    asyncio.run(run())


def test_progress_handles_history_cleared_between_batches(tmp_path):
    from tuner_under_test.worker import retention

    store = daystore.DayStore(tmp_path / "history")
    data = {"devices": {"a": {}}, "maintenance_progress": {"after": ["a", "2033-01-01"]}}
    batch = retention.snapshot(store, data, NOW)
    result = retention.execute(
        {"root": store.root, "directory": tmp_path, "data": data, "now": NOW, **batch}
    )
    assert not result["prepared"].get("maintenance_progress")
    assert result["changes"] == []


def test_scheduled_batches_continue_with_a_pause_and_failure_backoff(tmp_path, monkeypatch):
    async def run():
        runtime = runtime_at(tmp_path / "history")
        attempts, pauses = [], []

        async def clean():
            attempts.append(1)
            if len(attempts) == 1:
                runtime.data["maintenance_progress"] = {"after": ["a", "2033-01-01"]}
            else:
                runtime.data.pop("maintenance_progress")

        async def pause(seconds):
            pauses.append(seconds)

        runtime.async_clean_history = clean
        monkeypatch.setattr(recording.asyncio, "sleep", pause)
        monkeypatch.setattr(recording.label_counts, "ensure", AsyncMock())
        await recording._retain(runtime)
        assert len(attempts) == 2
        assert pauses == [60]
        runtime.data["maintenance_retry"] = {"after": NOW + policy.DAY}
        monkeypatch.setattr("time.time", lambda: NOW)
        await recording._retain(runtime)
        assert len(attempts) == 2
        recording.label_counts.ensure.assert_not_awaited()

    asyncio.run(run())


def test_retention_child_is_stdlib_only(tmp_path):
    import json
    import subprocess
    import sys

    script = "\n".join(
        [
            "import sys, types",
            "p = types.ModuleType('ld2410_child')",
            f"p.__path__ = [{json.dumps(str(test_tuner.ROOT))}]",
            "sys.modules[p.__name__] = p",
            "from ld2410_child.worker import retention",
            "assert retention._basis({'history_labels': []})['history_labels'] == []",
        ]
    )
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-B", "-c", script], capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr.decode()


def test_over_limit_routine_save_does_not_retry_failed_worker_hourly(tmp_path, monkeypatch):
    from tuner_under_test.worker.supervisor import WorkerFailed

    async def run():
        runtime = runtime_at(tmp_path / "history")
        runtime.data["storage_settings"] = {**policy.DEFAULTS, "max_mib": 1}
        runtime._days.root.mkdir()
        (runtime._days.root / "synthetic.index").write_bytes(b"x" * (2 * policy.MIB))
        attempts = []

        async def fail(*args, **kwargs):
            attempts.append(1)
            raise WorkerFailed(1, b"CPU time budget exhausted")

        monkeypatch.setattr(storage, "_save_days", fail)
        monkeypatch.setattr("time.time", lambda: NOW)
        with pytest.raises(WorkerFailed):
            await storage.async_save(runtime)
        assert runtime.data["maintenance_retry"]["after"] == NOW + policy.DAY
        with pytest.raises(budget.BudgetExceeded, match="backoff"):
            await storage.async_save(runtime)
        assert len(attempts) == 1
        with pytest.raises(WorkerFailed):
            await storage.trim(runtime)
        assert len(attempts) == 2

    asyncio.run(run())


def test_batch_without_cuts_preserves_saved_learning_and_histograms(tmp_path, monkeypatch):
    from tuner_under_test.worker.supervisor import Supervisor
    from worker_transport import Transport

    async def run():
        runtime = runtime_at(tmp_path / "history")
        runtime._days.root.mkdir()
        (runtime._days.root / "synthetic.index").write_bytes(b"x" * (8 * policy.MIB))
        runtime.data["devices"]["a"].update(histograms={"kept": {}}, last_learning={"id": "kept"})
        for age in range(1, 6):
            date = daystore.date_of(NOW - age * policy.DAY)
            low, high = daystore.bounds(date)
            builder = dayfile.DayBuilder(low, high)
            builder.add(dayfile.Change(low + 1, {0: 25}))
            builder.add(dayfile.End(high - 1))
            runtime._days.replace("a", date, builder.day())
        transport = Transport()

        async def process(service, args):
            return await transport.process(args)

        monkeypatch.setattr(Supervisor, "_process", process)
        monkeypatch.setattr("time.time", lambda: NOW)
        await recording.async_clean_history(runtime)
        await recording.async_clean_history(runtime)
        assert runtime.data["devices"]["a"]["last_learning"] == {"id": "kept"}
        assert runtime.data["devices"]["a"]["histograms"] == {"kept": {}}

    asyncio.run(run())


def stored_days(tmp_path, sizes):
    from tuner_under_test.worker import retention

    store = daystore.DayStore(tmp_path / "history")
    store.root.mkdir(parents=True)
    for age, size in enumerate(sizes, 1):
        date = daystore.date_of(NOW - age * policy.DAY)
        low, high = daystore.bounds(date)
        builder = dayfile.DayBuilder(low, high)
        builder.add(dayfile.Change(low + 1, {0: 25}))
        builder.add(dayfile.End(high - 1))
        store.replace("a", date, builder.day())
        store._path("a", date, ".day").write_bytes(b"x" * size)
    return retention, store


def test_a_batch_stops_at_its_byte_budget(tmp_path):
    limit = 1024
    retention, store = stored_days(tmp_path, [limit // 2 + 1] * 5)
    data = {"devices": {"a": {"history_labels": []}}}
    selected = retention.snapshot(store, data, NOW, max_bytes=limit)["batch"]["selected"]
    assert len(selected) == 1


def test_a_batch_always_takes_one_day_however_large(tmp_path):
    limit = 1024
    retention, store = stored_days(tmp_path, [limit * 3, 10])
    data = {"devices": {"a": {"history_labels": []}}}
    selected = retention.snapshot(store, data, NOW, max_bytes=limit)["batch"]["selected"]
    assert len(selected) == 1


def test_a_batch_of_small_days_stops_at_its_day_limit(tmp_path):
    retention, store = stored_days(tmp_path, [10] * (retention_days() + 2))
    data = {"devices": {"a": {"history_labels": []}}}
    selected = retention.snapshot(store, data, NOW)["batch"]["selected"]
    assert len(selected) == retention.BATCH_DAYS


def retention_days():
    from tuner_under_test.worker import retention

    return retention.BATCH_DAYS
