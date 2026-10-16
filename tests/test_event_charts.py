"""Native chart buckets preserve duration, gaps and exact pulse extrema."""

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import test_tuner  # noqa: F401
from tuner_under_test.history import dayfile, daystore
from tuner_under_test.presentation.charts import history_series_multi


def row(value, other=0):
    return (value, other, *([0] * 16))


def view(events, end=600, *, blocks=None, pending=False):
    temporary = tempfile.TemporaryDirectory(prefix="ld2410-charts-")
    days = daystore.DayStore(Path(temporary.name) / "history")
    for changes, stop in blocks if blocks is not None else [(events, end)]:
        for timestamp, values in changes:
            days.append("a", dayfile.Change(timestamp, dict(enumerate(values))))
        days.append("a", dayfile.End(stop))
    if not pending:
        days.flush(now=end)
    return SimpleNamespace(
        data={"devices": {"a": {"recording_enabled": True}}},
        _days=days,
        _history_until=max(end, blocks[-1][1]) if blocks else end,
        _temporary=temporary,
    )


def chart(runtime, *, end=600, hours=1, max_points=50, keys=("g0_move",)):
    with patch("time.time", return_value=end):
        return history_series_multi(runtime, "a", list(keys), hours, max_points, end)


@pytest.mark.parametrize("pending", [False, True])
def test_short_pulse_keeps_raw_extrema_exact_boundaries_and_duration_mean(pending):
    runtime = view(
        [(0.0, row(0.125)), (100.125, row(90.75)), (100.175, row(0.125))], pending=pending
    )
    result = chart(runtime)
    series = result["series"]["g0_move"]
    assert series["basis"] == "physical_span_duration"
    assert series["observed_seconds"] == 600
    peak = next(point for point in series["points"] if point["max"] == 90.75)
    assert peak["max_start"] == 100.125
    assert peak["max_end"] == 100.175
    assert peak["avg"] == pytest.approx(
        0.125 + (90.75 - 0.125) * 0.05 / (peak["end"] - peak["start"])
    )
    assert len(series["points"]) <= 50


def test_predecessor_clips_to_query_and_stable_signal_has_endpoint_anchors():
    result = chart(view([(0.0, row(10.125))], end=3600), end=3600, hours=0.1)
    points = result["series"]["g0_move"]["points"]
    assert points[0]["t"] == result["start"] == 3240
    assert points[-1]["t"] == result["end"] == 3600
    assert all(point["avg"] == point["min"] == point["max"] == 10.125 for point in points)
    assert sum(point["observed_seconds"] for point in points) == 360


def test_dense_unselected_gate_changes_do_not_change_requested_gate_average_or_support():
    sparse = [(0.0, row(10)), (100.0, row(40))]
    dense = [(i / 100, row(10, i % 100)) for i in range(1000)] + [(100.0, row(40))]
    one, many = chart(view(sparse)), chart(view(dense))
    assert one["series"]["g0_move"] == many["series"]["g0_move"]


def test_missing_gate_and_explicit_short_gap_create_breaks_inside_one_bucket():
    blocks = [
        ([(0.0, row(10))], 100.125),
        ([(100.175, row(20)), (101.0, row(None))], 600),
    ]
    result = chart(view([], blocks=blocks))
    series = result["series"]["g0_move"]
    assert (100.125, 100.175) in map(tuple, series["gaps"])
    assert (101.0, 600.0) in map(tuple, series["gaps"])
    following = next(point for point in series["points"] if point["start"] == 100.175)
    assert following["break_before"] is True
    assert series["observed_seconds"] == pytest.approx(101 - 0.05)
    assert all(point["end"] <= 100.125 or point["start"] >= 100.175 for point in series["points"])


def test_per_gate_missing_does_not_censor_other_gate_duration():
    runtime = view([(0.0, row(None, 10.5)), (100.0, row(20.25, 10.5))])
    result = chart(runtime, keys=("g0_move", "g0_still"))
    assert result["series"]["g0_move"]["observed_seconds"] == 500
    assert result["series"]["g0_still"]["observed_seconds"] == 600


def test_point_budget_omits_overfragmented_bucket_instead_of_joining_known_gaps():
    blocks = [([(i, row(10))], i + 0.5) for i in range(100)]
    blocks.append(([(120.0, row(30))], 600))
    result = chart(view([], blocks=blocks))
    series = result["series"]["g0_move"]
    assert len(series["points"]) <= 50
    assert series["omitted_ranges"]
    assert all(point["start"] >= 120 for point in series["points"])
    assert series["points"][0]["t"] >= 120
    assert series["observed_seconds"] == 530


def test_separate_coverage_ranges_use_physical_duration_without_bridging_gap():
    runtime = view([], end=20, blocks=[([(0.0, row(10))], 6), ([(10.0, row(20))], 20)])
    result = chart(runtime, end=20)
    assert result["modes"] == ["event"]
    assert result["series"]["g0_move"]["observed_seconds"] == 16
    assert (6.0, 10.0) in map(tuple, result["series"]["g0_move"]["gaps"])


def test_empty_day_store_never_reads_obsolete_json_history():
    runtime = view([], end=20, blocks=[])
    runtime.data["devices"]["a"]["history"] = [{"data": "retired"}]
    runtime._iter_history_samples = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("Chart must only read the day store")
    )
    result = chart(runtime, end=20)
    assert result["series"]["g0_move"]["observed_seconds"] == 0


def test_subsecond_extrema_timestamps_retain_large_epoch_precision():
    base = 1790500000.0
    result = chart(
        view(
            [(base, row(0.125)), (base + 0.125, row(80.25)), (base + 0.175, row(0.125))],
            end=base + 600,
        ),
        end=base + 600,
    )
    peak = next(point for point in result["series"]["g0_move"]["points"] if point["max"] == 80.25)
    assert peak["max_start"] == base + 0.125
    assert peak["max_end"] == base + 0.175
    assert peak["max_end"] > peak["max_start"]


def test_gap_diagnostics_remain_bounded_when_many_outages_are_omitted():
    blocks = [([(i, row(10))], i + 0.5) for i in range(1000)]
    result = chart(view([], blocks=blocks), end=1000)
    series = result["series"]["g0_move"]
    assert len(series["points"]) <= 50
    assert len(series["gaps"]) <= 50
    assert series["gaps_truncated"] is True
    assert len(series["omitted_ranges"]) <= 50
    assert series["observed_seconds"] == 500
