"""Synthetic raw-history workload contracts for full-worker scaling acceptance."""

import pytest
import test_tuner  # noqa: F401


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
