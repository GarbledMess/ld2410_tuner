"""Storage planning runs on a private tree and returns raw bounded handoffs."""

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_tuner  # noqa: F401
from tuner_under_test.history import budget, dayfile, daystore, policy


def test_batch_snapshot_carries_persistent_day_statistics(tmp_path):
    from tuner_under_test.worker import retention

    now = 2_000_000_000
    date = daystore.date_of(now - policy.DAY)
    store = daystore.DayStore(tmp_path / "history")
    builder = dayfile.DayBuilder(*daystore.bounds(date))
    builder.add(dayfile.Change(daystore.bounds(date)[0] + 1, {0: 25}))
    builder.add(dayfile.End(daystore.bounds(date)[0] + 2))
    store.replace("a", date, builder.day())
    for suffix in (".count", ".ret"):
        store._path("a", date, suffix).write_bytes(b"cache")
    data = {"devices": {"a": {"history_labels": []}}}
    paths = retention.snapshot(store, data, now)["_source_paths"]
    assert str(Path("a") / f"{date}.count") in paths
    assert str(Path("a") / f"{date}.ret") in paths


def test_failure_backoff_reports_previous_cause(tmp_path, monkeypatch):
    from tuner_under_test.history import storage

    runtime = SimpleNamespace(
        data={"devices": {}, "maintenance_retry": {"after": time.time() + 60}},
        _days=daystore.DayStore(tmp_path),
        _storage_status={"error": "MemoryError while counting day"},
        hass=SimpleNamespace(async_add_executor_job=asyncio.to_thread),
    )
    monkeypatch.setattr(storage, "_routine_report", lambda *_args: None)
    with pytest.raises(budget.BudgetExceeded, match="MemoryError while counting day"):
        asyncio.run(storage._prepare_and_save(runtime, None, False, None))


def test_retry_bookkeeping_failure_preserves_maintenance_error(monkeypatch):
    from tuner_under_test.history import live, storage

    runtime = SimpleNamespace(
        data={"devices": {}},
        _days=None,
        _storage_lock=asyncio.Lock(),
        _storage_status={},
    )

    async def fail_maintenance(*_args):
        raise ValueError("original maintenance failure")

    async def fail_retry(*_args):
        raise OSError("secondary retry save failure")

    monkeypatch.setattr(live, "settle", lambda _runtime: None)
    monkeypatch.setattr(storage, "_prepare_and_save", fail_maintenance)
    monkeypatch.setattr(storage, "retry_failure", fail_retry)
    with pytest.raises(ValueError, match="original maintenance failure"):
        asyncio.run(storage._persist(runtime))


def test_private_maintenance_matches_original_plan_without_publishing(tmp_path):
    from tuner_under_test.worker import maintenance, transactions

    original = daystore.DayStore(tmp_path / "original")
    now = 2_000_000_000
    timestamp = now - 10 * policy.DAY
    date = daystore.date_of(timestamp)
    builder = dayfile.DayBuilder(*daystore.bounds(date))
    builder.add(dayfile.Change(timestamp, {0: 25}))
    builder.add(dayfile.End(timestamp + 6))
    original.replace("a", date, builder.day())
    data = {"devices": {"a": {"history_labels": []}}}
    expected, changes, report = budget.prepare_days(original, data, now, clean=True)
    # The first planning pass creates optional sidecars. Measure the stable source tree.
    expected, changes, report = budget.prepare_days(original, data, now, clean=True)
    directory = tmp_path / "job"
    transactions.copy_tree(original.root, directory / "history")
    before = original._path("a", date, ".day").read_bytes()
    result = maintenance.execute(
        {
            "root": str(directory / "history"),
            "directory": str(directory),
            "data": data,
            "now": now,
            "clean": True,
            "target": None,
            "live": {},
        }
    )
    assert {item["stage"] for item in result["profile"]} >= {
        "open",
        "retention",
        "histograms",
        "stage",
    }
    assert all(item["wall_seconds"] >= 0 and item["cpu_seconds"] >= 0 for item in result["profile"])
    assert all("rss_kib" in item for item in result["profile"])
    assert all("peak_rss_kib" not in item for item in result["profile"])
    prepared, actual, actual_report, staged = maintenance.load(result, directory)
    assert prepared == expected
    assert {key: value for key, value in actual_report.items() if key != "used_bytes"} == {
        key: value for key, value in report.items() if key != "used_bytes"
    }
    private_store = daystore.DayStore(directory / "history")
    replacement_delta = sum(
        path.stat().st_size - private_store._path(key, date, ".day").stat().st_size
        for key, date, _day, path in staged
    )
    assert actual_report["used_bytes"] == (
        private_store.size() + budget.storage_bytes(prepared) + replacement_delta
    )
    assert len(actual) == len(changes) == len(staged) == 1
    assert actual[0][:2] == changes[0][:2]
    assert actual[0][2] == changes[0][2]
    assert Path(staged[0][3]).exists()
    assert original._path("a", date, ".day").read_bytes() == before


def test_maintenance_uses_no_decoded_day_cache(tmp_path, monkeypatch):
    from tuner_under_test.worker import maintenance

    seen = []
    original = maintenance.daystore.DayStore

    def capture(root, **kwargs):
        instance = original(root, **kwargs)
        seen.append(instance)
        return instance

    monkeypatch.setattr(maintenance.daystore, "DayStore", capture)
    directory = tmp_path / "job"
    result = maintenance.execute(
        {
            "root": str(directory / "history"),
            "directory": str(directory),
            "data": {"devices": {}, "storage_settings": {**policy.DEFAULTS, "max_mib": 512}},
            "now": 2_000_000_000,
            "clean": True,
        }
    )
    assert result["profile"]
    assert seen[0]._cache_bytes == 0


def test_maintenance_handoff_keeps_replacements_on_disk(tmp_path, monkeypatch):
    from tuner_under_test.worker import maintenance

    now = 2_000_000_000
    store = daystore.DayStore(tmp_path / "job" / "history")
    for offset in range(1, 9):
        timestamp = now - offset * policy.DAY
        date = daystore.date_of(timestamp)
        builder = dayfile.DayBuilder(*daystore.bounds(date))
        builder.add(dayfile.Change(timestamp, {0: 25}))
        builder.add(dayfile.End(timestamp + 6))
        store.replace("a", date, builder.day())

    def refuse_large_handoff(*_args, **_kwargs):
        raise AssertionError("decoded day handoff allocated")

    monkeypatch.setattr(maintenance.startup, "write_day", refuse_large_handoff)
    result = maintenance.execute(
        {
            "root": str(store.root),
            "directory": str(store.root.parent),
            "data": {"devices": {"a": {"history_labels": []}}},
            "now": now,
            "clean": True,
        }
    )
    assert result["changes"]
    assert all("day" not in entry for entry in result["changes"])
    _prepared, changes, _report, staged = maintenance.load(result, store.root.parent)
    assert len(changes) == len(staged) == len(result["changes"])
    assert all(isinstance(day, daystore.StagedDay) for _key, _date, day in changes)


def test_histogram_recount_reuses_unchanged_day_statistics(tmp_path, monkeypatch):
    store = daystore.DayStore(tmp_path / "history")
    now = 2_000_000_000
    for offset in (2, 3):
        timestamp = now - offset * policy.DAY
        date = daystore.date_of(timestamp)
        builder = dayfile.DayBuilder(*daystore.bounds(date))
        builder.add(dayfile.Change(timestamp, {0: 25}))
        builder.add(dayfile.End(timestamp + 6))
        store.replace("a", date, builder.day())
    device = {"history_labels": [{"start": now - 4 * policy.DAY, "end": now, "state": "present"}]}
    original = budget._day_histograms(store, "a", device, {}, now, 0)

    def refuse_read(*_args, **_kwargs):
        raise AssertionError("unchanged day decoded")

    monkeypatch.setattr(store, "day", refuse_read)
    assert budget._day_histograms(store, "a", device, {}, now + 1, 0) == original
    assert "_maintenance_day_counts" not in device


def test_count_sidecars_transfer_only_for_unchanged_days(tmp_path, monkeypatch):
    from tuner_under_test.history import maintenance_cache
    from tuner_under_test.worker import transactions

    now = 2_000_000_000
    original = daystore.DayStore(tmp_path / "original")
    timestamp = now - 2 * policy.DAY
    date = daystore.date_of(timestamp)
    builder = dayfile.DayBuilder(*daystore.bounds(date))
    builder.add(dayfile.Change(timestamp, {0: 25}))
    builder.add(dayfile.End(timestamp + 6))
    original.replace("a", date, builder.day())
    device = {"history_labels": [{"start": timestamp, "end": now, "state": "present"}]}
    private_root = tmp_path / "private"
    transactions.copy_tree(original.root, private_root)
    private = daystore.DayStore(private_root)
    private.reconcile()
    expected = budget._day_histograms(private, "a", device, {}, now, 0)
    expected_retention = budget._retention(private, "a", date, device, persist=True)
    assert not list(original.root.rglob("*.count"))
    assert not list(original.root.rglob("*.ret"))
    maintenance_cache.transfer(private_root, original.root)
    assert list(original.root.rglob("*.count"))
    assert list(original.root.rglob("*.ret"))

    def refuse_read(*_args, **_kwargs):
        raise AssertionError("unchanged source day decoded")

    monkeypatch.setattr(original, "day", refuse_read)
    assert budget._day_histograms(original, "a", device, {}, now, 0) == expected
    assert budget._retention(original, "a", date, device, persist=True) == expected_retention


def test_replanning_a_day_reuses_its_spool_file(tmp_path):
    now = 2_000_000_000
    timestamp = now - 10 * policy.DAY
    date = daystore.date_of(timestamp)
    store = daystore.DayStore(tmp_path / "history")
    builder = dayfile.DayBuilder(*daystore.bounds(date))
    builder.add(dayfile.Change(timestamp, {0: 25}))
    builder.add(dayfile.End(timestamp + 6))
    store.replace("a", date, builder.day())
    spool = tmp_path / "spool"
    planned = budget._Planned(store, None, {}, spool=spool)
    planned.cut(("a", date), [(timestamp, timestamp + 1)])
    planned.cut(("a", date), [(timestamp, timestamp + 2)])
    budget._Planned(store, None, {("a", date): [(timestamp, timestamp + 2)]}, spool=spool)
    assert len(list(spool.iterdir())) == 1


def test_size_pressure_preserves_count_sidecars_needed_for_recount(tmp_path):
    from tuner_under_test.worker import maintenance

    store = daystore.DayStore(tmp_path / "history")
    path = store.root / "a" / "2033-05-15.count"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"cached statistics")
    (path.parent / "2033-05-15.day").write_bytes(b"x" * 1000)
    maintenance._prune(store, {"devices": {}}, 100, store.root)
    assert path.exists()


def test_ceiling_plan_does_not_reencode_for_every_candidate(tmp_path, monkeypatch):
    from array import array

    store = daystore.DayStore(tmp_path / "history")
    date = daystore.date_of(2_000_000_000)
    start, end = daystore.bounds(date)
    count = 512
    day = dayfile.Day(
        start,
        end,
        array("d", (start + 6 * index for index in range(count))),
        array("d", (start + 6 * (index + 1) for index in range(count))),
        [bytes((index * 47 + gate * 13) % 101 for index in range(count)) for gate in range(18)],
        [],
    )
    store.replace("a", date, day)
    data = {"devices": {"a": {"history_labels": []}}}
    candidates = [
        (False, 0, start + 6 * index, "a", date, start + 6 * (index + 1)) for index in range(count)
    ]
    monkeypatch.setattr(budget, "_day_candidates", lambda *_args, **_kwargs: iter(candidates))
    original_encode = dayfile.encode_day
    calls = 0

    def counted_encode(day):
        nonlocal calls
        calls += 1
        return original_encode(day)

    monkeypatch.setattr(dayfile, "encode_day", counted_encode)
    target = store.size() // 2
    cuts = {}
    budget._ceiling_plan(store, data, cuts, target, spool=tmp_path / "spool")
    assert cuts
    assert calls < 40


def test_uniform_labelled_day_counts_without_per_row_spans(tmp_path, monkeypatch):
    from array import array

    from tuner_under_test.history import spans

    store = daystore.DayStore(tmp_path / "history")
    date = daystore.date_of(2_000_000_000)
    start, end = daystore.bounds(date)
    rows = 10_000
    day = dayfile.Day(
        start,
        end,
        array("d", (start + index for index in range(rows))),
        array("d", (start + index + 1 for index in range(rows))),
        [bytes([25]) * rows for _ in range(18)],
        [],
    )
    store.replace("a", date, day)
    device = {"history_labels": [{"start": start, "end": end, "state": "present"}]}

    def refuse_spans(*_args, **_kwargs):
        raise AssertionError("per-row spans traversed")

    monkeypatch.setattr(spans, "day_spans", refuse_spans)
    result = budget._day_histograms(store, "a", device, {}, end, 0)
    assert result["g0_move"]["present"][25] == pytest.approx(rows / 6)


def test_day_statistic_caches_invalidate_for_labels_cutoff_and_rewrite(tmp_path):
    now = 2_000_000_000
    timestamp = now - 2 * policy.DAY
    date = daystore.date_of(timestamp)
    store = daystore.DayStore(tmp_path / "history")
    builder = dayfile.DayBuilder(*daystore.bounds(date))
    builder.add(dayfile.Change(timestamp, {0: 25}))
    builder.add(dayfile.End(timestamp + 6))
    store.replace("a", date, builder.day())
    present = {"history_labels": [{"start": timestamp, "end": timestamp + 6, "state": "present"}]}
    absent = {
        "history_labels": [{"start": timestamp, "end": timestamp + 6, "state": "not_present"}]
    }
    unknown = {"history_labels": []}

    first = budget._day_histograms(store, "a", present, {}, now, 0)
    second = budget._day_histograms(store, "a", absent, {}, now, 0)
    assert first["g0_move"]["present"][25] == 1
    assert second["g0_move"]["not_present"][25] == 1
    assert budget._retention(store, "a", date, present, persist=True) != budget._retention(
        store, "a", date, unknown, persist=True
    )
    clipped = budget._day_histograms(store, "a", present, {}, now, timestamp + 3)
    assert clipped["g0_move"]["present"][25] == 0.5

    rewritten = dayfile.DayBuilder(*daystore.bounds(date))
    rewritten.add(dayfile.Change(timestamp, {0: 40}))
    rewritten.add(dayfile.End(timestamp + 6))
    store.replace("a", date, rewritten.day())
    after = budget._day_histograms(store, "a", present, {}, now, 0)
    assert after["g0_move"]["present"][25] == 0
    assert after["g0_move"]["present"][40] == 1


def test_many_recorded_days_keep_handoff_small_and_counts_exact(tmp_path):
    from array import array

    from tuner_under_test.worker import maintenance

    now = 2_000_000_000
    rows = 20_000
    store = daystore.DayStore(tmp_path / "job" / "history")
    for offset in (*range(1, 30), 31):
        date = daystore.date_of(now - offset * policy.DAY)
        start, end = daystore.bounds(date)
        day = dayfile.Day(
            start,
            end,
            array("d", (start + index * 0.25 for index in range(rows))),
            array("d", (start + (index + 1) * 0.25 for index in range(rows))),
            [bytes([25]) * rows for _ in range(18)],
            [],
        )
        store.replace("a", date, day)
    result = maintenance.execute(
        {
            "root": str(store.root),
            "directory": str(store.root.parent),
            "data": {
                "devices": {
                    "a": {
                        "history_labels": [
                            {"start": now - 30 * policy.DAY, "end": now, "state": "present"}
                        ]
                    }
                },
                "storage_settings": {**policy.DEFAULTS, "max_mib": 512},
            },
            "now": now,
            "clean": True,
        }
    )
    assert len(result["changes"]) == 1
    assert "day" not in result["changes"][0]
    assert result["counts"]["a"]["seconds"]["g0_move"]["present"][25] == (29 * rows * 0.25)


def test_handoff_rejects_path_outside_private_job(tmp_path):
    from tuner_under_test.worker import maintenance

    with pytest.raises(ValueError, match="path"):
        maintenance.load(
            {
                "prepared": {},
                "report": {},
                "changes": [
                    {"key": "a", "date": "1970-01-01", "temporary": "../outside", "day": {}}
                ],
            },
            tmp_path,
        )


@pytest.mark.parametrize("advance", [0, 19])
def test_live_handoff_reopens_run_without_duplicating_recovery_close(tmp_path, advance):
    from tuner_under_test.worker import maintenance, transactions

    now = 2_000_000_000
    start = now - 100
    original = daystore.DayStore(tmp_path / "original")
    original.append("a", dayfile.Change(start, {0: 25}))
    original.append("a", dayfile.Guess(start, "present", 16, 0))
    if advance:
        original.append("a", dayfile.Change(start + advance, {0: 30}))
    original.flush(now)
    live = original._live["a"]
    expected = original.day("a", live.date, until=now)
    directory = tmp_path / "job"
    transactions.copy_tree(original.root, directory / "history")
    recovered = daystore.DayStore(directory / "history")
    recovered.open(now)
    maintenance._restore_live(
        recovered,
        {
            "a": {
                "date": str(live.date),
                "last": live.last,
                "seen": live.seen,
                "current": list(live.builder.current),
                "run": list(live.builder.run),
            }
        },
    )
    actual = recovered.day("a", live.date, until=now)
    assert actual == expected
    assert actual.guesses == [(start, now, "present", 16, 0)]
    device = {"history_labels": [{"start": start, "end": now, "state": "present"}]}
    assert list(budget._held_ranges(actual, device)) == list(budget._held_ranges(expected, device))
    assert budget._day_histograms(recovered, "a", device, {}, now, 0) == budget._day_histograms(
        original, "a", device, {}, now, 0
    )
