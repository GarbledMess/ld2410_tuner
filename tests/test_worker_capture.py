"""Capture rejects oversized work before copying mutable column buffers."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest
import test_tuner  # noqa: F401
from test_event_fitting import view
from test_event_quality import block
from tuner_under_test.history import dayfile, worker_snapshot
from tuner_under_test.worker import calculations, capture


def run(function, *args, **kwargs):
    with ThreadPoolExecutor() as executor:
        return executor.submit(function, *args, **kwargs).result()


def test_capture_requires_executor(tmp_path):
    with pytest.raises(RuntimeError, match="executor"):
        capture.prepare(view([], []), "any-name", tmp_path)


@pytest.mark.parametrize("field", list(vars(capture.CaptureLimits())))
def test_invalid_limits(field):
    with pytest.raises(ValueError):
        replace(capture.CaptureLimits(), **{field: 0})


@pytest.mark.parametrize("budget", [{"max_rows": 1}, {"max_raw_bytes": 1}])
def test_oversized_mutable_history_rejected_before_writes(tmp_path, budget):
    source = view([block(0, 600, 40), block(600, 1200, 0)], [])
    with pytest.raises(worker_snapshot.SnapshotTooLarge):
        run(
            capture.prepare,
            source,
            "any-name",
            tmp_path,
            limits=replace(capture.CaptureLimits(), **budget),
        )
    assert list(tmp_path.iterdir()) == []


def test_aggregate_rows_across_independent_stores_rejected_before_writes(tmp_path):
    first = view([block(0, 600, 40)], [])
    second = view([block(0, 600, 40)], [])
    with pytest.raises(worker_snapshot.SnapshotTooLarge):
        run(
            capture.prepare_multi,
            {"a": first, "b": second},
            tmp_path,
            limits=replace(capture.CaptureLimits(), max_rows=1),
        )
    assert list(tmp_path.iterdir()) == []


def test_capture_finished_reference_keeps_pending_late_guesses(tmp_path):
    source = view([block(0, 600, 40)], [])
    source._days.append("any-name", dayfile.End(86400))
    source._days.flush()
    span = dayfile.GuessSpan(90000, 0, 600, "present", 20, 1)
    source._days._late.setdefault("any-name", {})[source._days.days("any-name")[0]] = [span]
    request = run(capture.prepare, source, "any-name", tmp_path)
    restored = calculations.restore(request)
    date = restored._days.days("any-name")[0]
    assert restored._days.day("any-name", date).guesses == [(0, 600, "present", 20, 1)]


def test_capture_no_store_and_metadata_is_detached(tmp_path):
    source = SimpleNamespace(data={"devices": {"any-name": {"history_labels": []}}})
    request = run(capture.prepare, source, "any-name", tmp_path)
    source.data["devices"]["any-name"]["history_labels"].append({"state": "present"})
    assert calculations.restore(request).data["devices"]["any-name"]["history_labels"] == []


def test_production_capture_accepts_history_beyond_old_device_and_day_counts(tmp_path):
    import datetime

    from tuner_under_test.history import daystore

    store = daystore.DayStore(tmp_path / "history")
    devices = {f"radar{index}": {} for index in range(17)}
    for key in devices:
        for offset in range(33):
            date = datetime.date(1970, 1, 1) + datetime.timedelta(days=offset)
            path = store._path(key, date, ".day")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(dayfile.encode_day(dayfile.DayBuilder(*daystore.bounds(date)).day()))
    # Refresh public finished-file inventory after constructing the disk fixture.
    store = daystore.DayStore(store.root)
    run(store.open, 40 * 86400)
    source = SimpleNamespace(data={"devices": devices}, _days=store)
    request = run(capture.prepare, source, "all", tmp_path / "capture")
    assert len(request["days"]) == 17 * 33


@pytest.mark.parametrize("multiple", [False, True])
def test_device_or_view_cardinality_rejected_before_acquisition(tmp_path, multiple):
    source = SimpleNamespace(data={"devices": {"a": {}, "b": {}}})
    limits = replace(capture.CaptureLimits(), max_devices=1)
    with pytest.raises(worker_snapshot.SnapshotTooLarge, match="view" if multiple else "device"):
        if multiple:
            run(capture.prepare_multi, {"a": source, "b": source}, tmp_path, limits=limits)
        else:
            run(capture.prepare, source, "a", tmp_path, limits=limits)
    assert list(tmp_path.iterdir()) == []


def finished_source(root, device_ids, offsets=(0,)):
    """Real immutable files, so rejected captures cannot hide behind fake store methods."""
    from tuner_under_test.history import daystore

    store = daystore.DayStore(root)
    for device_id in device_ids:
        for offset in offsets:
            date = daystore.date_of(offset)
            builder = dayfile.DayBuilder(*daystore.bounds(date))
            builder.add(dayfile.Change(offset + 100, {0: 40}))
            builder.add(dayfile.End(offset + 106))
            store.replace(device_id, date, builder.day())
    return SimpleNamespace(data={"devices": dict.fromkeys(device_ids, {})}, _days=store)


@pytest.mark.parametrize("multiple", [False, True])
def test_day_limits_reject_single_and_combined_view_history(tmp_path, multiple):
    private = tmp_path / "private"
    limits = replace(capture.CaptureLimits(), max_days=1)
    if multiple:
        first = finished_source(tmp_path / "first", ["a"])
        second = finished_source(tmp_path / "second", ["b"], (86400,))
        operation, args = capture.prepare_multi, ({"a": first, "b": second}, private)
        reason = "Aggregate worker day"
    else:
        source = finished_source(tmp_path / "history", ["a"], (0, 86400))
        operation, args = capture.prepare, (source, "a", private)
        reason = "Worker day"
    with pytest.raises(worker_snapshot.SnapshotTooLarge, match=reason):
        run(operation, *args, limits=limits)
    assert not private.exists()


def test_device_limit_is_aggregate_across_individually_valid_views(tmp_path):
    private = tmp_path / "private"
    first = finished_source(tmp_path / "first", ["a", "b"])
    second = finished_source(tmp_path / "second", ["c"])
    with pytest.raises(worker_snapshot.SnapshotTooLarge, match="Aggregate worker device"):
        run(
            capture.prepare_multi,
            {"first": first, "second": second},
            private,
            limits=replace(capture.CaptureLimits(), max_devices=2),
        )
    assert not private.exists()


@pytest.mark.parametrize("multiple", [False, True])
def test_file_bytes_reject_single_and_combined_immutable_captures(tmp_path, multiple):
    private = tmp_path / "private"
    first = finished_source(tmp_path / "first", ["a"])
    physical = next((tmp_path / "first").rglob("*.day"))
    original = physical.read_bytes()
    size = len(original)
    if multiple:
        second = finished_source(tmp_path / "second", ["b"])
        other = next((tmp_path / "second").rglob("*.day"))
        limit = max(size, other.stat().st_size)
        assert size + other.stat().st_size > limit
        operation, args = capture.prepare_multi, ({"a": first, "b": second}, private)
        reason = "Aggregate worker file"
    else:
        limit = size - 1
        operation, args = capture.prepare, (first, "a", private)
        reason = "Aggregate history file"
    with pytest.raises(worker_snapshot.SnapshotTooLarge, match=reason):
        run(operation, *args, limits=replace(capture.CaptureLimits(), max_file_bytes=limit))
    assert physical.read_bytes() == original
    assert list(private.iterdir()) == []


def test_chart_window_capture_slices_bst_live_day_without_changing_geometry(tmp_path):
    import datetime
    from pathlib import Path

    from test_event_charts import row
    from test_event_charts import view as chart_view
    from tuner_under_test.presentation.charts import history_series_multi
    from tuner_under_test.worker import codec, display

    end = datetime.datetime.fromisoformat("2026-10-22T00:20:00+01:00").timestamp()
    midnight = int(end // 86400) * 86400
    source = chart_view(
        [(midnight + offset, row(offset % 200)) for offset in range(0, int(end - midnight), 100)],
        end=end,
        pending=True,
    )
    source._snapshot_history_start, source._history_until = end - 1200, end
    expected = history_series_multi(source, "a", ["g0_move"], 1 / 3, 1000, end)
    source._capture_chart_window = True
    request = run(capture.prepare, source, "a", tmp_path / "short")
    captured = calculations.restore(request)
    date = captured._days.days("a")[0]
    day = captured._days.day("a", date)
    assert len(day.times) <= 13  # Twenty minutes, rather than 23h20 of UTC live columns.
    request.update(
        operation="charts",
        parameters=codec.encode(
            {"device_id": "a", "keys": ["g0_move"], "hours": 1 / 3, "max_points": 1000, "end": end}
        ),
    )
    assert display.unpack_chart(calculations.execute(request)) == expected
    with pytest.raises(worker_snapshot.SnapshotTooLarge):
        run(
            capture.prepare,
            source,
            "a",
            tmp_path / "reject",
            limits=replace(capture.CaptureLimits(), max_rows=20),
        )
    assert not (tmp_path / "reject").exists()
    first = SimpleNamespace(**vars(source))
    second = SimpleNamespace(**vars(source))
    second._snapshot_history_start = end - 600
    requests = run(capture.prepare_multi, {"one": first, "two": second}, tmp_path / "multi")
    paths = [item["days"][0]["path"] for item in requests["views"].values()]
    assert len(set(paths)) == 2
    assert all(Path(path).is_file() for path in paths)


def test_chart_capture_keeps_open_predecessor_gaps_and_historical_cutoffs(tmp_path):
    from test_event_charts import row
    from test_event_charts import view as chart_view
    from tuner_under_test.presentation.charts import history_series_multi
    from tuner_under_test.worker import codec, display

    source = chart_view([(0, row(10)), (100, row(30)), (200, row(50))], end=300, pending=True)
    source._days.append("a", dayfile.Change(400, {0: 60}))
    for index, (start, end) in enumerate(((450, 810), (-110, 250), (90, 450))):
        source._capture_chart_window = True
        source._snapshot_history_start, source._history_until = start, end
        parameters = {
            "device_id": "a",
            "keys": ["g0_move"],
            "hours": 0.1,
            "max_points": 1000,
            "end": end,
        }
        expected = history_series_multi(source, **parameters)
        request = run(capture.prepare, source, "a", tmp_path / str(index))
        request.update(operation="charts", parameters=codec.encode(parameters))
        actual = display.unpack_chart(calculations.execute(request))
        assert actual == expected
