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


def _reference_chart(runtime, keys, *, end, hours, max_points, summary):
    """The per-span algorithm the column runs must reproduce."""
    from tuner_under_test.const import HISTORY_KEYS
    from tuner_under_test.history import spans
    from tuner_under_test.presentation import charts

    start, indices = end - hours * 3600, {key: HISTORY_KEYS.index(key) for key in keys}
    bucket = next(s for s in (6, 12, 30, 60, 120, 300, 600) if s >= (end - start) / max_points)
    while 2 * (charts.math.ceil(end / bucket) - charts.math.floor(start / bucket)) > max_points:
        bucket *= 2
    series = (
        {} if summary else {k: charts._NativeSeries(start, end, bucket, max_points) for k in keys}
    )
    strongest = charts._NativeSeries(start, end, bucket, max_points) if summary else None
    modes = set()
    for cell in spans.events(runtime, "a", start=start, end=end):
        modes.add(cell.mode)
        charts._add_native_span(series, strongest, indices, cell)
    if summary:
        return strongest.finish()["points"]
    return {key: value.finish() for key, value in series.items()}, sorted(modes)


def _random_blocks(seed, fractional):
    import random

    rng = random.Random(seed)
    blocks, clock = [], 0.0
    for _ in range(4):
        changes = []
        for _ in range(rng.randrange(1, 400)):
            clock += rng.choice((0.0, 0.05, 0.5, 1.0, 1.0, 2.0, 7.5))
            gates = rng.sample(range(18), rng.randrange(1, 4))
            values = {
                gate: None
                if rng.random() < 0.05
                else rng.randrange(0, 100) + (rng.choice((0, 0.25)) if fractional else 0)
                for gate in gates
            }
            changes.append((clock, values))
        clock += rng.choice((0.5, 30.0))
        blocks.append((changes, clock))
        clock += rng.choice((0.0, 3.0, 400.0))
    return blocks, clock


def _blocks_view(blocks, end):
    temporary = tempfile.TemporaryDirectory(prefix="ld2410-charts-")
    days = daystore.DayStore(Path(temporary.name) / "history")
    for changes, stop in blocks:
        for timestamp, values in changes:
            days.append("a", dayfile.Change(timestamp, values))
        days.append("a", dayfile.End(stop))
    days.flush(now=end)
    return SimpleNamespace(
        data={"devices": {"a": {"recording_enabled": True}}},
        _days=days,
        _history_until=end,
        _temporary=temporary,
    )


def _close(actual, expected):
    if isinstance(expected, dict):
        assert set(actual) == set(expected)
        for key in expected:
            _close(actual[key], expected[key])
    elif isinstance(expected, (list, tuple)):
        assert len(actual) == len(expected)
        for a, b in zip(actual, expected, strict=True):
            _close(a, b)
    elif isinstance(expected, float):
        assert actual == pytest.approx(expected, abs=1e-3)
    else:
        assert actual == expected


@pytest.mark.parametrize("seed", range(12))
@pytest.mark.parametrize("fractional", [False, True])
@pytest.mark.parametrize("summary", [False, True])
def test_column_runs_match_per_span_chart(seed, fractional, summary):
    blocks, stop = _random_blocks(seed, fractional)
    keys = ("g0_move", "g3_move", "g5_still", "g8_still")
    end = stop - (seed % 3) * 7.25  # Query ends inside recorded rows as well as after them.
    hours = (end - (seed % 4) * 11.5) / 3600  # And starts inside a row.
    runtime = _blocks_view(blocks, end)
    with patch("time.time", return_value=end):
        result = history_series_multi(runtime, "a", list(keys), hours, 120, end, summary)
        expected = _reference_chart(
            runtime, keys, end=end, hours=hours, max_points=120, summary=summary
        )
    if summary:
        _close(result["points"], expected)
    else:
        _close(result["series"], expected[0])
        assert result["modes"] == expected[1]


@pytest.mark.parametrize("summary", [False, True])
def test_whole_number_day_is_charted_from_columns_without_rebuilding_rows(summary):
    changes = [(i * 0.5, {i % 18: i % 101}) for i in range(20000)]
    runtime = _blocks_view([(changes, 10000.0)], 10000.0)
    keys = [f"g{gate}_{kind}" for gate in range(9) for kind in ("move", "still")]
    with (
        patch("time.time", return_value=10000.0),
        patch.object(dayfile.Day, "vector", side_effect=AssertionError("per-row vector rebuilt")),
    ):
        result = history_series_multi(runtime, "a", keys, 10000 / 3600, 400, 10000.0, summary)
    assert result["points" if summary else "series"]
