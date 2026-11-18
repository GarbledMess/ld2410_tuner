"""Storage keeps recording and keeps cleaning itself through the failures a home meets."""

import asyncio
import random
import unittest
from copy import deepcopy
from unittest.mock import patch

import test_tuner as harness
from tuner_under_test.history import budget, dayfile, daystore, ingest, policy, recording, storage


class RecordingThroughMaintenanceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        harness.RuntimeTests.setUp(self)

    asyncTearDown = harness.RuntimeTests.asyncTearDown
    sample = harness.RuntimeTests.sample

    def _old_day(self):
        old = self.now - 10 * policy.DAY
        self.sample(old)
        ingest.end(self.runtime, "a", old + 6)
        return daystore.date_of(old)

    async def test_daily_batches_finish_while_the_radar_keeps_recording(self):
        date = self._old_day()
        self.sample(self.now - 6)
        recorded = 0

        async def record(request):
            nonlocal recorded
            if request["operation"] == "maintenance":
                recorded += 1
                self.sample(self.now + recorded * 6, 40 + recorded)

        self.worker_transport.after = record
        with patch.object(storage, "_batched", lambda *_args: True):
            await self.runtime.async_clean_history()

        self.assertEqual(recorded, 1, "One batch, accepted although the radar recorded")
        self.assertIsNone(self.runtime._days.day("a", date))
        self.assertNotIn("maintenance_progress", self.runtime.data)
        self.assertNotIn("maintenance_retry", self.runtime.data)
        self.assertGreaterEqual(self.runtime._days.seen("a"), self.now + 5.9)

    async def test_labelled_time_recorded_during_a_batch_is_counted_not_overwritten(self):
        from tuner_under_test.history import labels

        self._old_day()
        self.device["history_labels"] = [
            {"start": self.now - 60, "end": self.now + 600, "state": "present"}
        ]
        self.sample(self.now - 12)
        self.sample(self.now - 6)
        counted = []

        async def record(request):
            if request["operation"] == "maintenance":
                self.sample(self.now + 6, 70)  # Labelled: the live histograms grow.
                counted.append(deepcopy(self.device["histograms"]))

        self.worker_transport.after = record
        rebuilt = []
        with (
            patch.object(storage, "_batched", lambda *_args: True),
            patch.object(labels, "_rebuild_soon", lambda _runtime, key: rebuilt.append(key)),
        ):
            await self.runtime.async_clean_history()
        self.assertEqual(rebuilt, ["a"])
        self.assertEqual(self.device["histograms"], counted[-1])

    async def test_a_passing_failure_never_pauses_recording(self):
        self.sample(self.now - 100)
        self.runtime.data["storage_settings"] = {**policy.DEFAULTS, "max_mib": 1}

        async def times_out(*_args, **_kwargs):
            raise TimeoutError("maintenance worker timed out")

        with patch.object(budget, "storage_bytes", lambda _data: 2 * policy.MIB):
            with patch.object(storage, "_save_days", times_out):
                with self.assertRaises(TimeoutError):
                    await self.runtime.async_save()
            # Backing off: the refusal neither pauses recording nor nests its message.
            with self.assertRaises(budget.BudgetExceeded):
                await self.runtime.async_save()
        self.assertFalse(self.runtime._storage_status.get("blocked"))
        self.assertTrue(ingest._accepting(self.runtime, "a"))
        self.assertEqual(self.runtime.data["maintenance_retry"]["delay"], storage.FIRST_RETRY)

    async def test_only_an_impossible_limit_pauses_recording(self):
        self.sample(self.now - 100)
        self.runtime.data["storage_settings"] = {**policy.DEFAULTS, "max_mib": 1}

        async def impossible(*_args, **_kwargs):
            raise budget.LimitImpossible(budget.IMPOSSIBLE)

        with patch.object(budget, "storage_bytes", lambda _data: 2 * policy.MIB):
            with patch.object(storage, "_save_days", impossible):
                with self.assertRaises(budget.LimitImpossible):
                    await self.runtime.async_save()
            with self.assertRaises(budget.BudgetExceeded):
                await self.runtime.async_save()  # Backing off keeps it paused.
        self.assertTrue(self.runtime._storage_status.get("blocked"))
        self.assertFalse(ingest._accepting(self.runtime, "a"))


def test_backoff_starts_at_an_hour_and_never_skips_a_daily_cleanup():
    data, delays = {}, []
    for _failure in range(8):
        delay = storage.next_delay(data)
        delays.append(delay)
        data["maintenance_retry"] = storage.backoff(delay)
    assert delays[0] == 3600
    assert delays == sorted(delays)
    assert max(delays) == recording.CLEAN_EVERY


def _day(store, key, date, step, rows):
    start, _end = daystore.bounds(date)
    builder = dayfile.DayBuilder(*daystore.bounds(date))
    readings = random.Random(date.toordinal())  # Readings that do not compress to nothing.
    for index in range(rows):
        builder.add(
            dayfile.Change(
                start + index * step, {gate: readings.randrange(101) for gate in range(18)}
            )
        )
    builder.add(dayfile.End(start + rows * step))
    store.replace(key, date, builder.day())
    return start, start + rows * step


def _store(tmp_path, now, days=6, rows=3000):
    store = daystore.DayStore(tmp_path / "history")
    spans = [
        _day(store, "a", daystore.date_of(now - (days - index) * policy.DAY), 6, rows)
        for index in range(days)
    ]
    return store, spans


def test_an_automatic_size_trim_leaves_headroom_below_the_ceiling(tmp_path):
    now = 2_000_000_000
    store, _spans = _store(tmp_path, now)
    data = {"devices": {"a": {"history_labels": []}}}
    before = store.size() + budget.storage_bytes(data)
    ceiling = int(before * 0.8)
    data["storage_settings"] = {**policy.DEFAULTS, "max_mib": ceiling / policy.MIB}
    ceiling = int(policy.settings(data)["max_mib"] * policy.MIB)
    _prepared, changes, report = budget.prepare_days(store, data, now)
    assert changes
    assert report["used_bytes"] <= budget.headroom(ceiling)


def test_headroom_never_costs_human_labelled_time(tmp_path):
    """Automatic time alone reaches the ceiling, not its headroom: the labelled days stay."""
    now = 2_000_000_000
    store, spans = _store(tmp_path, now, days=5, rows=12000)
    labels = [{"start": low, "end": high, "state": "present"} for low, high in spans[1:]]
    data = {"devices": {"a": {"history_labels": labels}}}
    before = store.size() + budget.storage_bytes(data)
    # The size once all automatic time (the oldest day) is gone, as a trim leaves it.
    automatic = {("a", daystore.date_of(spans[0][0])): [spans[0]]}
    planned = budget._Planned(store, None, automatic)
    changes, _automatic, _human = budget._replacement_days(store, data, automatic, planned)
    prepared = budget.copy_data(data)
    budget._day_metadata(store, prepared, changes, now)  # Its histograms are saved too.
    without_automatic = planned.total_with(prepared)
    # That fits the ceiling, but not the 10% below it.
    ceiling = without_automatic + (before - without_automatic) // 4
    assert budget.headroom(ceiling) < without_automatic < ceiling < before
    data["storage_settings"] = {**policy.DEFAULTS, "max_mib": ceiling / policy.MIB}
    _prepared, changes, report = budget.prepare_days(store, data, now)
    assert report["removed_human_seconds"] == 0
    assert report["removed_automatic_seconds"] > 0
    assert report["used_bytes"] <= ceiling
    assert [date for _key, date, _day in changes] == [daystore.date_of(spans[0][0])]


def test_startup_removes_private_trees_left_by_unfinished_transactions(tmp_path):
    from tuner_under_test.worker import startup, transactions

    root = tmp_path / "ld2410_tuner" / "history"
    root.mkdir(parents=True)
    (root / "a").mkdir()
    kept = root / "a" / "2033-05-15.day"
    kept.write_bytes(b"history")
    left = tmp_path / "ld2410_tuner" / (transactions.PRIVATE_PREFIX + "abc")
    (left / "history" / "a").mkdir(parents=True)
    (left / "history" / "a" / "2033-05-15.day").write_bytes(b"copy")
    other = tmp_path / "ld2410_tuner" / "unrelated"
    other.mkdir()
    startup.recover_publication(root)
    assert not left.exists()
    assert kept.read_bytes() == b"history"
    assert other.exists()


def test_routine_save_over_the_ceiling_is_not_repeated_every_save(tmp_path):
    """After an automatic trim the next minutes of recording fit again."""
    now = 2_000_000_000
    store, _spans = _store(tmp_path, now)
    data = {"devices": {"a": {"history_labels": []}}}
    before = store.size() + budget.storage_bytes(data)
    data["storage_settings"] = {**policy.DEFAULTS, "max_mib": before * 0.9 / policy.MIB}
    ceiling = int(policy.settings(data)["max_mib"] * policy.MIB)
    _prepared, _changes, report = budget.prepare_days(store, data, now)
    assert ceiling - report["used_bytes"] >= 0.09 * ceiling


if __name__ == "__main__":
    asyncio.run(unittest.main())


def test_a_size_trim_still_applies_the_age_expiry(tmp_path):
    """The size plan builds on the age plan: an expired human-labelled day goes even when
    automatic time alone brings the store within the ceiling."""
    now = 2_000_000_000
    store = daystore.DayStore(tmp_path / "history")
    expired = _day(store, "a", daystore.date_of(now - 40 * policy.DAY), 6, 6000)
    for ago in (3, 2, 1):
        _day(store, "a", daystore.date_of(now - ago * policy.DAY), 6, 6000)
    data = {
        "devices": {
            "a": {"history_labels": [{"start": expired[0], "end": expired[1], "state": "present"}]}
        }
    }
    before = store.size() + budget.storage_bytes(data)
    data["storage_settings"] = {**policy.DEFAULTS, "max_mib": before * 0.95 / policy.MIB}
    _prepared, changes, report = budget.prepare_days(store, data, now)
    assert daystore.date_of(expired[0]) in {date for _key, date, _day in changes}
    assert report["removed_human_seconds"] > 0
    assert report["removed_automatic_seconds"] > 0
