"""History date inventories used by synchronous HA snapshots never touch disk."""

import asyncio
import os
import shutil
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_tuner as harness
from test_daystore import DAY, NEXT, START, change
from tuner_under_test.history import dayfile, daystore


def memory_days(store, expected):
    async def check():
        with (
            patch.object(Path, "exists", side_effect=AssertionError("exists on HA loop")),
            patch.object(Path, "is_file", side_effect=AssertionError("is_file on HA loop")),
            patch.object(Path, "stat", side_effect=AssertionError("stat on HA loop")),
            patch.object(Path, "iterdir", side_effect=AssertionError("iterdir on HA loop")),
            patch.object(os, "scandir", side_effect=AssertionError("scandir on HA loop")),
        ):
            assert store.days("radar") == expected

    asyncio.run(check())


def test_finished_history_inventory_is_loaded_at_startup_without_loop_io(tmp_path):
    store = daystore.DayStore(tmp_path)
    store.append("radar", change(START + 1, g0=10))
    store.append("radar", dayfile.End(START + 2))
    store.flush(NEXT + 1)
    recovered = daystore.DayStore(tmp_path)
    recovered.open(NEXT + 1)
    memory_days(recovered, [DAY])


def test_inventory_tracks_publication_delete_and_clear(tmp_path):
    store = daystore.DayStore(tmp_path)
    day = dayfile.DayBuilder(START, NEXT)
    day.add(change(START + 1, g0=10))
    day.add(dayfile.End(START + 2))
    store.replace("radar", DAY, day.day())
    memory_days(store, [DAY])
    store.delete("radar", [DAY])
    memory_days(store, [])
    store.replace("radar", DAY, day.day())
    store.forget_device("radar")
    memory_days(store, [])
    store._remove_files("radar")
    memory_days(store, [])


def test_inventory_preserves_accepted_views_until_publication(tmp_path):
    store = daystore.DayStore(tmp_path)
    builder = dayfile.DayBuilder(START, NEXT)
    builder.add(change(START + 1, g0=10))
    builder.add(dayfile.End(START + 2))
    store.replace("radar", DAY, builder.day())
    empty = dayfile.DayBuilder(START, NEXT).day()
    staged = store.stage([("radar", DAY, empty)])
    reserved = store.reserve(staged)
    memory_days(store, [])
    store.abort_reserved(reserved)
    memory_days(store, [DAY])
    reserved = store.reserve(staged)
    store.publish_reserved(reserved)
    memory_days(store, [])


def test_finished_empty_log_is_removed_from_inventory(tmp_path):
    store = daystore.DayStore(tmp_path)
    store.append("radar", dayfile.End(START + 1))
    store.flush(START + 2)
    store.flush(NEXT + 1)
    memory_days(store, [DAY + timedelta(days=1)])


def test_startup_transaction_hands_finished_inventory_back_to_owner(tmp_path):
    from tuner_under_test.worker import startup

    root, job = tmp_path / "history", tmp_path / "job"
    source = daystore.DayStore(root)
    source.append("radar", change(START + 1, g0=10))
    source.append("radar", dayfile.End(START + 2))
    source.flush(NEXT + 1)
    shutil.copytree(root, job / "history")
    data = {"devices": {}}
    result = startup.execute(
        {"root": str(job / "history"), "directory": str(job), "data": data, "now": NEXT + 2}
    )
    owner = daystore.DayStore(root)
    startup.publish(SimpleNamespace(_days=owner, data=data), owner, result, job)
    memory_days(owner, [DAY])


def test_worker_flush_keeps_finished_dates_after_closing_ack(tmp_path):
    from test_worker_flushing import transaction
    from tuner_under_test.worker import flushing

    owner = daystore.DayStore(tmp_path / "history")
    owner.root.mkdir()
    owner.append("radar", change(START + 1, g0=10))
    owner.append("radar", dayfile.End(START + 2))
    result, ownership = transaction(owner, tmp_path / "job", NEXT + 2)
    flushing.publish(owner, result, ownership, tmp_path / "job")
    memory_days(owner, [DAY, DAY + timedelta(days=1)])


def test_reconcile_ignores_unrelated_files_and_refreshes_deleted_days(tmp_path):
    store = daystore.DayStore(tmp_path)
    folder = store.folder("radar")
    folder.mkdir()
    (folder / "unrelated.day").write_bytes(b"")
    (folder / "2025-10-15.day").mkdir()
    (folder / "2025-10-16.log").write_bytes(b"")
    store.reconcile()
    memory_days(store, [DAY + timedelta(days=1)])
    (folder / "2025-10-16.log").unlink()
    store.reconcile()
    memory_days(store, [])


class SnapshotInventoryTests(unittest.IsolatedAsyncioTestCase):
    setUp = harness.RuntimeTests.setUp
    asyncTearDown = harness.RuntimeTests.asyncTearDown

    async def test_full_snapshot_of_restarted_finished_history_never_touches_filesystem(self):
        from tuner_under_test.presentation import snapshots

        entity = "sensor.radar_g0_move_energy"
        self.registry.entities[entity] = SimpleNamespace(
            entity_id=entity, device_id="a", domain="sensor", disabled_by=None
        )
        store = self.runtime._days
        store.append("a", change(START + 1, g0=10))
        store.append("a", dayfile.End(START + 2))
        await asyncio.to_thread(store.flush, NEXT + 1)
        self.runtime._days = daystore.DayStore(store.root)
        await asyncio.to_thread(self.runtime._days.open, NEXT + 2)
        with (
            patch.object(Path, "exists", side_effect=AssertionError("exists on HA loop")),
            patch.object(Path, "is_file", side_effect=AssertionError("is_file on HA loop")),
            patch.object(Path, "stat", side_effect=AssertionError("stat on HA loop")),
            patch.object(Path, "iterdir", side_effect=AssertionError("iterdir on HA loop")),
            patch.object(os, "scandir", side_effect=AssertionError("scandir on HA loop")),
        ):
            result = snapshots.snapshot(self.runtime)
        assert result["devices"]["a"]["history"]["days"] == 1
        assert result["devices"]["a"]["history"]["oldest_day"] == DAY.isoformat()
