"""Synthetic raw-history workload contracts for full-worker scaling acceptance."""

import pytest
import test_tuner  # noqa: F401


def _capacity():
    import runpy
    from pathlib import Path
    from types import SimpleNamespace

    return SimpleNamespace(
        **runpy.run_path(str(Path(__file__).parent / "lab/hybrid_capacity_checks.py"))
    )


def test_capacity_cpu_verdict_keeps_the_pinned_desktop_budgets():
    capacity = _capacity()
    assert capacity.budget_failures("cpu30", {"cpu_seconds": 10}) == []
    assert capacity.budget_failures("cpu30", {"cpu_seconds": 11}) == ["cpu_seconds > 10"]
    assert capacity.budget_failures("warm7", {"cpu_seconds": 2}) == []
    assert capacity.budget_failures("warm7", {"cpu_seconds": 3}) == ["cpu_seconds > 2"]
    assert capacity.budget_failures("memory30", {"traced_peak_bytes": 64 * 1024**2}) == [
        "traced_peak_bytes >= 67108864"
    ]


def test_capacity_report_records_failed_jobs_and_continues(tmp_path):
    capacity = _capacity()
    calls = []

    def run(root, **dimensions):
        calls.append(dimensions)
        if len(calls) == 1:
            raise RuntimeError("CPU time budget exhausted")
        return {**_capacity_result(), "cpu_seconds": 3}

    output = tmp_path / "report.json"
    report = capacity.measure(tmp_path / "jobs", output, ["cpu30", "warm7"], run)
    assert len(calls) == 2
    assert calls[0] == {"radars": 30, "days": 28}
    assert calls[1] == {"radars": 7, "days": 28, "warm": True}
    assert report["cases"]["cpu30"]["status"] == "failed"
    assert report["cases"]["cpu30"]["error"] == "CPU time budget exhausted"
    assert report["cases"]["warm7"]["failures"] == ["cpu_seconds > 2"]
    assert not report["passed"]
    import json

    assert json.loads(output.read_text()) == report


def _capacity_result():
    return {
        "cpu_seconds": 1,
        "isolated": True,
        "no_site": True,
        "no_bytecode": True,
        "legality": True,
        "max_row_block": 4096,
        "resident_digests_at_end": 0,
        "open_spill_files": 0,
        "thresholds": {"r0": {"g0_move": 50}},
        "cost_before": 2,
        "cost_after": 1,
    }


@pytest.mark.parametrize(
    ("key", "value", "reason"),
    [
        ("isolated", False, "child isolation flags missing"),
        ("legality", False, "illegal thresholds"),
        ("max_row_block", 4097, "raw block exceeds 4096 rows"),
        ("resident_digests_at_end", 1, "digests retained after job"),
        ("open_spill_files", 1, "spill files retained after job"),
    ],
)
def test_capacity_report_rejects_invalid_or_leaking_jobs(key, value, reason):
    result = {**_capacity_result(), key: value}
    assert reason in _capacity().job_failures(result)


def test_capacity_report_rejects_changed_warm_results():
    result = _capacity_result()
    result["cold"] = {**result, "cost_after": 0}
    assert _capacity().job_failures(result) == ["cold/warm fitting results differ"]
    result["cold"]["cost_after"] = result["cost_after"]
    assert _capacity().job_failures(result) == []


def test_scaling_failed_child_removes_captured_inputs(tmp_path, monkeypatch):
    import asyncio

    import hybrid_scaling
    from tuner_under_test.worker.supervisor import WorkerFailed

    fixture = hybrid_scaling.build_fixture(tmp_path, radars=1, days=2)
    request = hybrid_scaling._request

    def invalid_request(*args):
        path = request(*args)
        path.write_bytes(b"invalid request")
        return path

    monkeypatch.setattr(hybrid_scaling, "_request", invalid_request)
    with pytest.raises(WorkerFailed, match="ProtocolRejected"):
        asyncio.run(hybrid_scaling._run_fixture(fixture, tmp_path, 2, False, False))
    assert not list(tmp_path.glob("scaling-job-*"))
    assert list((tmp_path / "history").iterdir())


def test_scaling_cancelled_child_is_reaped_before_inputs_are_removed(tmp_path, monkeypatch):
    import asyncio

    import hybrid_scaling

    fixture = hybrid_scaling.build_fixture(tmp_path, radars=1, days=1)
    create = asyncio.create_subprocess_exec
    processes = []

    async def run():
        spawned = asyncio.Event()

        async def capture(*args, **kwargs):
            process = await create(*args, **kwargs)
            processes.append(process)
            spawned.set()
            return process

        monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
        task = asyncio.create_task(hybrid_scaling._run_fixture(fixture, tmp_path, 1, False, False))
        try:
            await asyncio.wait_for(spawned.wait(), 30)
            assert list(tmp_path.glob("scaling-job-*/history.sqlite"))
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert processes[0].returncode is not None
            assert not list(tmp_path.glob("scaling-job-*"))
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_scaling_cancelled_capture_finishes_before_directory_cleanup(tmp_path, monkeypatch):
    import asyncio
    import threading

    import hybrid_scaling

    fixture = hybrid_scaling.build_fixture(tmp_path, radars=1, days=1)
    request = hybrid_scaling._request
    release = threading.Event()

    async def run():
        loop = asyncio.get_running_loop()
        captured = asyncio.Event()

        def blocked_capture(*args):
            path = request(*args)
            loop.call_soon_threadsafe(captured.set)
            if not release.wait(30):
                raise TimeoutError("Test did not release capture")
            assert path.exists()
            return path

        monkeypatch.setattr(hybrid_scaling, "_request", blocked_capture)
        task = asyncio.create_task(hybrid_scaling._run_fixture(fixture, tmp_path, 1, False, False))
        try:
            await asyncio.wait_for(captured.wait(), 30)
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            assert list(tmp_path.glob("scaling-job-*/request.json"))
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not list(tmp_path.glob("scaling-job-*"))
        finally:
            release.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_scaling_fixture_covers_full_days_and_keeps_a_live_day(tmp_path):
    from hybrid_scaling import DAY, START, build_fixture

    fixture = build_fixture(tmp_path, radars=2, days=2)
    assert START % DAY == 0
    assert fixture["radar_days"] == 4
    assert fixture["base_cadence_seconds"] == 6
    assert fixture["labelled_duty_cycle"] == pytest.approx(1 / 3)
    assert fixture["source_rows"] > 4 * (DAY // 6 - 100)
    store = fixture["store"]
    for rid in ("r0", "r1"):
        dates = store.days(rid)
        assert len(dates) == 2
        assert store.live_date(rid) == dates[-1]
        for date in dates:
            day = store.day(rid, date, until=START + 2 * DAY)
            assert day.times[0] == day.start
            assert day.ends[-1] == day.end
            assert len(day.columns) == 18
            assert all(len(set(column)) > 2 for column in day.columns)
            durations = [b - a for a, b in zip(day.times, day.ends, strict=True)]
            assert 0.125 in durations
            assert sum(durations) == DAY - 60
        labels = fixture["devices"][rid]["history_labels"]
        assert len(labels) >= 8
        assert sum(item["end"] - item["start"] for item in labels) == 2 * DAY / 3


def test_scaling_fixture_is_reproducible_and_density_preserves_coverage(tmp_path):
    from hybrid_scaling import DAY, START, build_fixture

    fixtures = [
        build_fixture(tmp_path / name, radars=1, days=1, density=density)
        for name, density in (("a", 1), ("b", 1), ("dense", 2))
    ]
    days = [
        item["store"].day("r0", item["store"].days("r0")[0], until=START + DAY) for item in fixtures
    ]
    assert days[0] == days[1]
    assert fixtures[0]["devices"] == fixtures[1]["devices"]
    assert len(days[2].times) > len(days[0].times) * 1.9
    coverage = [sum(b - a for a, b in zip(day.times, day.ends, strict=True)) for day in days]
    assert coverage[0] == coverage[2]


def test_scaling_fixture_contains_correlated_and_opposite_response_radars(tmp_path):
    from hybrid_scaling import DAY, START, build_fixture

    fixture = build_fixture(tmp_path, radars=3, days=1)
    vectors = []
    for rid in ("r0", "r1", "r2"):
        store = fixture["store"]
        day = store.day(rid, store.days(rid)[0], until=START + DAY)
        vectors.append([day.vector(day.times.index(START + hour * 3600 + 1800)) for hour in (6, 8)])
    for channel in range(18):
        assert vectors[0][0][channel] > vectors[0][1][channel] + 20
        assert vectors[1] == vectors[0]
        assert vectors[2][0][channel] + 20 < vectors[2][1][channel]


@pytest.mark.parametrize("field", ["radars", "days", "density"])
@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_scaling_fixture_rejects_invalid_dimensions(tmp_path, field, value):
    from hybrid_scaling import build_fixture

    with pytest.raises(ValueError, match=field):
        build_fixture(tmp_path, **{field: value})


def test_scaling_runs_complete_cold_job_in_a_fresh_child(tmp_path):
    import os
    import sys

    from hybrid_scaling import run_scaling

    result = run_scaling(tmp_path, radars=1, days=1, trace=True)
    assert result["pid"] != os.getpid()
    assert result["isolated"] and result["no_site"] and result["no_bytecode"]
    assert result["mode"] == "cold"
    assert result["radar_days"] == 1
    assert result["source_rows"] == 14580
    assert result["cpu_seconds"] > 0
    assert result["traced_peak_bytes"] > 0
    assert result["legality"]
    assert len(result["thresholds"]["r0"]) == 18
    assert result["cost_before"] >= 0
    assert result["cost_after"] >= 0
    assert result["raw_rows_read"] >= result["source_rows"]
    assert 0 < result["max_row_block"] <= 4096
    assert result["cache_hits"] == 0
    assert result["spill_bytes"] >= 0
    assert result["open_spill_files"] == 0
    assert 0 < result["max_resident_digests"] <= 3
    assert result["resident_digests_at_end"] == 0
    assert 0 < result["resident_digest_bytes"] < 4 * 1024**2
    if sys.platform == "linux":
        assert result["limits"]["memory_bytes"] == 512 * 1024**2
        assert result["limits"]["cpu_seconds"] == 120
        assert result["limits"]["file_bytes"] == 128 * 1024**2
        assert result["limits"]["open_files"] == 64
    else:
        assert result["limits"] is None
    assert not list(tmp_path.glob("scaling-job-*"))


def test_scaling_warm_job_preserves_thresholds_and_base_costs(tmp_path):
    from hybrid_scaling import run_scaling

    result = run_scaling(tmp_path, radars=1, days=2, warm=True)
    assert result["mode"] == "warm"
    assert result["thresholds"] == result["cold"]["thresholds"]
    assert result["cost_before"] == result["cold"]["cost_before"]
    assert result["cost_after"] == result["cold"]["cost_after"]
    assert result["pid"] != result["cold"]["pid"]
    assert result["cache_hits"] > 0
    assert result["raw_rows_read"] < result["cold"]["raw_rows_read"]
    assert result["cold"]["raw_rows_read"] >= result["source_rows"]
    assert result["traced_peak_bytes"] == 0
    assert not list(tmp_path.glob("scaling-job-*"))


def test_scaling_captures_default_dimensions_without_protocol_overflow(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    from hybrid_scaling import DAY, START, _labels, _request
    from tuner_under_test.history import dayfile, daystore
    from tuner_under_test.worker import calculations, manifest, protocol

    store = daystore.DayStore(tmp_path / "history", cache_bytes=0)
    labels = [label for day in range(28) for label in _labels(START + day * DAY)]
    devices = {f"r{i}": {"history_labels": labels, "training_state": "unknown"} for i in range(30)}
    for rid in devices:
        for index in range(28):
            start = START + index * DAY
            builder = dayfile.DayBuilder(start, start + DAY)
            builder.add(dayfile.Change(start, dict.fromkeys(range(18), 20)))
            builder.add(dayfile.End(start + DAY))
            store.replace(rid, daystore.date_of(start), builder.day())
    directory = tmp_path / "job"
    directory.mkdir()
    with ThreadPoolExecutor(max_workers=1) as executor:
        path = executor.submit(
            _request, {"store": store, "devices": devices}, directory, 28
        ).result()
    request = protocol.unpack(path.read_bytes(), "learn")
    restored = calculations.restore(manifest.expand(request)["views"]["all"])
    assert len(restored._days.entries) == 30 * 28
    assert len(restored.data["devices"]) == 30
    assert all(len(restored._days.days(rid)) == 28 for rid in devices)


def test_scaling_spill_metrics_measure_concurrent_disk_bytes(monkeypatch):
    import tempfile

    import hybrid_scaling

    with monkeypatch.context() as patch:
        patch.setattr(tempfile, "SpooledTemporaryFile", tempfile.SpooledTemporaryFile)
        patch.setattr(tempfile, "TemporaryFile", tempfile.TemporaryFile)
        metrics = hybrid_scaling._measure_spills()
        with tempfile.SpooledTemporaryFile(max_size=8) as first:
            first.write(b"1234")
            assert metrics["spill_bytes"] == 0
            first.write(b"56789")
            assert metrics["spill_bytes"] == 9
            with tempfile.SpooledTemporaryFile(max_size=8) as second:
                second.write(b"abcdefghij")
                assert metrics["spill_bytes"] == 19
                assert metrics["max_spill_files"] == 2
            assert metrics["open_spill_files"] == 1
        assert metrics["open_spill_files"] == 0
        assert metrics["spill_bytes"] == 19


def test_scaling_spill_metrics_include_unspooled_replay_streams(monkeypatch):
    import tempfile

    import hybrid_scaling

    with monkeypatch.context() as patch:
        patch.setattr(tempfile, "TemporaryFile", tempfile.TemporaryFile)
        patch.setattr(tempfile, "SpooledTemporaryFile", tempfile.SpooledTemporaryFile)
        metrics = hybrid_scaling._measure_spills()
        with tempfile.TemporaryFile() as stream:
            stream.write(b"replay")
            assert metrics["spill_bytes"] == 6
            assert metrics["open_spill_files"] == 1
            stream.truncate(20)
            assert metrics["spill_bytes"] == 20
            stream.truncate(4)
            with tempfile.TemporaryFile() as other:
                other.write(b"0123456789")
                assert metrics["spill_bytes"] == 20
        assert metrics["open_spill_files"] == 0
